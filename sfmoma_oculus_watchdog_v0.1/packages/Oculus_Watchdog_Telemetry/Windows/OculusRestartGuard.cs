using System;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

internal static class OculusRestartGuard
{
    private const string ServiceName = "OculusRestartGuard";
    private const uint Stop = 1, Interrogate = 4, PreShutdown = 15;
    private const uint Stopped = 1, Starting = 2, Stopping = 3, Running = 4;
    private const uint InvalidDataError = 13;
    private const uint AcceptStop = 0x01, AcceptPreShutdown = 0x100;

    private static readonly ManualResetEvent StopRequested = new ManualResetEvent(false);
    private static readonly ServiceMain MainCallback = RunService;
    private static readonly ControlHandler ControlCallback = OnControl;
    private static IntPtr statusHandle;
    private static int stopControl;
    private static long sequence;
    private static readonly string StreamId = Guid.NewGuid().ToString("N");

    private delegate void ServiceMain(int argc, IntPtr argv);
    private delegate uint ControlHandler(uint control, uint eventType, IntPtr eventData, IntPtr context);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ServiceEntry
    {
        [MarshalAs(UnmanagedType.LPWStr)] public string Name;
        [MarshalAs(UnmanagedType.FunctionPtr)] public ServiceMain Run;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ServiceStatus
    {
        public uint Type, State, Controls, ExitCode, ServiceExitCode, Checkpoint, WaitHint;
    }

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool StartServiceCtrlDispatcher([In] ServiceEntry[] table);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr RegisterServiceCtrlHandlerEx(string name, ControlHandler handler, IntPtr context);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool SetServiceStatus(IntPtr handle, ref ServiceStatus status);

    private static bool Publish(uint state, uint exitCode = 0)
    {
        bool pending = state == Starting || state == Stopping;
        var status = new ServiceStatus
        {
            Type = 0x10,
            State = state,
            Controls = state == Running ? AcceptStop | AcceptPreShutdown : 0,
            ExitCode = exitCode,
            Checkpoint = pending ? 1U : 0U,
            WaitHint = pending ? 5000U : 0U,
        };
        return SetServiceStatus(statusHandle, ref status);
    }

    private static uint OnControl(uint control, uint eventType, IntPtr eventData, IntPtr context)
    {
        if (control == Stop || control == PreShutdown)
        {
            Interlocked.CompareExchange(ref stopControl, (int)control, 0);
            Publish(Stopping);
            StopRequested.Set();
            return 0;
        }
        return control == Interrogate ? 0U : 120U;
    }

    private static void RunService(int argc, IntPtr argv)
    {
        statusHandle = RegisterServiceCtrlHandlerEx(ServiceName, ControlCallback, IntPtr.Zero);
        if (statusHandle == IntPtr.Zero || !Publish(Starting))
            return;

        try
        {
            string[] config = File.ReadAllLines(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "guard.config"));
            if (config.Length != 4 || Environment.MachineName.ToUpperInvariant() != "CP007-01")
                throw new InvalidDataException();

            IPAddress mac = IPAddress.Parse(config[0]);
            IPAddress windows = IPAddress.Parse(config[1]);
            int port = Int32.Parse(config[2], CultureInfo.InvariantCulture);
            if (mac.AddressFamily != AddressFamily.InterNetwork ||
                windows.AddressFamily != AddressFamily.InterNetwork || port < 1024 || port > 65535)
                throw new InvalidDataException();

            byte[] key = ParseKey(config[3]);
            var source = new IPEndPoint(windows, 0);
            var destination = new IPEndPoint(mac, port);
            if (!Publish(Running))
                return;

            while (!StopRequested.WaitOne(0))
            {
                Send(source, destination, key, "ready");
                StopRequested.WaitOne(TimeSpan.FromSeconds(3));
            }

            if (Volatile.Read(ref stopControl) == PreShutdown)
            {
                for (int i = 0; i < 4; i++)
                {
                    Send(source, destination, key, "stopping");
                    if (i < 3) Thread.Sleep(150);
                }
            }
        }
        catch
        {
            Publish(Stopped, InvalidDataError);
            return;
        }
        Publish(Stopped);
    }

    private static void Send(IPEndPoint source, IPEndPoint destination, byte[] key, string phase)
    {
        byte[] data = Packet(key, "CP007-01", StreamId, ++sequence, DateTimeOffset.UtcNow.ToUnixTimeSeconds(), phase);
        try
        {
            using (var udp = new UdpClient(source))
            {
                udp.Client.Blocking = false;
                udp.Send(data, data.Length, destination);
            }
        }
        catch (SocketException)
        {
        }
    }

    private static byte[] ParseKey(string hex)
    {
        if (!Regex.IsMatch(hex, @"\A[a-fA-F0-9]{64}\z"))
            throw new InvalidDataException();
        byte[] bytes = new byte[32];
        for (int i = 0; i < bytes.Length; i++)
            bytes[i] = Convert.ToByte(hex.Substring(i * 2, 2), 16);
        return bytes;
    }

    private static byte[] Packet(byte[] key, string host, string stream, long seq, long stamp, string phase)
    {
        string json = String.Format(CultureInfo.InvariantCulture,
            "{{\"schema\":\"oculus.windows.lifecycle\",\"version\":1,\"host\":\"{0}\",\"stream_id\":\"{1}\",\"sequence\":{2},\"sent_unix\":{3},\"phase\":\"{4}\"}}",
            host, stream, seq, stamp, phase);
        byte[] payload = Encoding.UTF8.GetBytes(json);
        using (var hmac = new HMACSHA256(key))
        {
            string digest = BitConverter.ToString(hmac.ComputeHash(payload)).Replace("-", "").ToLowerInvariant();
            return Encoding.UTF8.GetBytes("{\"payload_b64\":\"" + Convert.ToBase64String(payload) +
                                          "\",\"hmac_sha256\":\"" + digest + "\"}");
        }
    }

    private static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--self-test")
        {
            byte[] key = new byte[32];
            for (int i = 0; i < key.Length; i++) key[i] = (byte)i;
            string wire = Encoding.UTF8.GetString(Packet(key, "CP007-01", new string('a', 32), 1, 1700000000, "stopping"));
            return wire.Contains("ec86fe62625ae06ffa64a83676b4f5c262d38fd6e20a4b9183688a14e4fc2a24") ? 0 : 1;
        }

        ServiceEntry[] dispatcher = {new ServiceEntry { Name = ServiceName, Run = MainCallback }, new ServiceEntry()};
        return StartServiceCtrlDispatcher(dispatcher) ? 0 : Marshal.GetLastWin32Error();
    }
}
