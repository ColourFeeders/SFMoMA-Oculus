using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

internal static class StartObserver
{
    private const int ExtendedLimitInformation = 9;
    private const uint KillOnJobClose = 0x2000;

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicJobLimits
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperations, WriteOperations, OtherOperations;
        public ulong ReadBytes, WriteBytes, OtherBytes;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedJobLimits
    {
        public BasicJobLimits BasicLimits;
        public IoCounters Io;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemory, PeakJobMemory;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, IntPtr name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedJobLimits limits, uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    private static void TieObserverToTask()
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, IntPtr.Zero);
        if (job == IntPtr.Zero)
            throw new Win32Exception();
        var limits = new ExtendedJobLimits();
        limits.BasicLimits.LimitFlags = KillOnJobClose;
        if (!SetInformationJobObject(job, ExtendedLimitInformation, ref limits,
                                     (uint)Marshal.SizeOf(typeof(ExtendedJobLimits))) ||
            !AssignProcessToJobObject(job, GetCurrentProcess()))
        {
            var error = new Win32Exception();
            CloseHandle(job);
            throw error;
        }
    }

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length < 2 || args.Length > 3 ||
                (args.Length == 3 && args[2] != "-Quiet" && args[2] != "-SelfTest") ||
                !File.Exists(args[0]) || !File.Exists(args[1]))
                throw new ArgumentException("Expected observer script and configuration files.");

            TieObserverToTask();
            var start = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe"),
                Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + args[0] +
                            "\" -ConfigPath \"" + args[1] + "\"" + (args.Length == 3 ? " " + args[2] : ""),
                WorkingDirectory = Path.GetDirectoryName(args[0]),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardError = true,
                RedirectStandardOutput = true,
            };
            using (Process observer = Process.Start(start))
            {
                observer.BeginOutputReadLine();
                string error = observer.StandardError.ReadToEnd();
                observer.WaitForExit();
                if (observer.ExitCode != 0)
                    WriteError("PowerShell exit code " + observer.ExitCode + ". " + error);
                return observer.ExitCode;
            }
        }
        catch (Exception error)
        {
            WriteError(error.Message);
            return 1;
        }
    }

    private static void WriteError(string message)
    {
        try
        {
            string state = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                        @"SFMoMAOculus\WatchdogObserver");
            Directory.CreateDirectory(state);
            File.WriteAllText(Path.Combine(state, "observer-launch-error.txt"),
                              DateTime.UtcNow.ToString("o") + " " + message.Substring(0, Math.Min(2048, message.Length)));
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
