# Credentials and deployment boundaries

Keep the HMAC host-heartbeat key, PiKVM credentials/certificate fingerprint, SSH private key, pinned SSH host key, dashboard power token, account passwords and live state outside version control. Use the existing private credential-transfer process; never paste these into issues, commits or CI variables for this repository.

The host heartbeat uses a shared signing key; the Windows and Mac copies must match. PiKVM authentication and certificate pinning are configured interactively on the Mac. The dashboard binds locally and uses a local power token. SSH is restricted to the dedicated standard account and the approved Mac source IP; the Windows handler accepts a narrow set of power/status actions. Review the component implementation before modifying those boundaries.

Diagnostics and TD network exports may contain usernames, file paths, project logic, installed software, network information or environment values. Review them before sharing and do not commit them. The client PDF is an internal operations document and is excluded from this repository.

Do not expose the watchdog UDP ports, PiKVM management interface or Windows power SSH endpoint to the public internet. Network changes belong in the venue's controlled configuration process.

For incidents, preserve logs privately, disable auto-recovery when safe, and contact the project's designated engineering owner. No support email or vulnerability-report recipient was supplied for this handoff.
