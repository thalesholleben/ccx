using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

internal static class CcxCodexBridgeLauncher
{
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string name);

    [DllImport("kernel32.dll")]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int informationClass,
        IntPtr information,
        uint informationLength);

    [DllImport("kernel32.dll")]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    private static IntPtr CreateKillOnCloseJob()
    {
        var job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
            return IntPtr.Zero;
        var information = new ExtendedLimitInformation();
        information.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
        var length = Marshal.SizeOf(typeof(ExtendedLimitInformation));
        var pointer = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(information, pointer, false);
            if (!SetInformationJobObject(job, 9, pointer, (uint)length))
            {
                CloseHandle(job);
                return IntPtr.Zero;
            }
            return job;
        }
        finally
        {
            Marshal.FreeHGlobal(pointer);
        }
    }

    private static string Quote(string value)
    {
        if (value.Length > 0 && value.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            return value;

        var output = new StringBuilder();
        output.Append('"');
        var slashes = 0;
        foreach (var ch in value)
        {
            if (ch == '\\')
            {
                slashes++;
                continue;
            }
            if (ch == '"')
            {
                output.Append('\\', slashes * 2 + 1);
                output.Append('"');
                slashes = 0;
                continue;
            }
            output.Append('\\', slashes);
            slashes = 0;
            output.Append(ch);
        }
        output.Append('\\', slashes * 2);
        output.Append('"');
        return output.ToString();
    }

    public static int Main(string[] args)
    {
        try
        {
            var executable = Assembly.GetExecutingAssembly().Location;
            var configPath = Path.ChangeExtension(executable, ".cfg");
            var config = File.ReadAllLines(configPath, Encoding.UTF8);
            if (config.Length != 3 || config[0].Length == 0 || config[1].Length == 0 || config[2].Length == 0)
                return 78;

            var allArguments = new List<string> { Quote(config[1]) };
            foreach (var arg in args)
                allArguments.Add(Quote(arg));

            var start = new ProcessStartInfo
            {
                FileName = config[0],
                Arguments = string.Join(" ", allArguments.ToArray()),
                UseShellExecute = false,
            };
            start.EnvironmentVariables["CCX_CODEX_REAL"] = config[2];
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            var job = CreateKillOnCloseJob();
            if (job == IntPtr.Zero)
                return 70;
            try
            {
                using (var child = Process.Start(start))
                {
                    if (child == null)
                        return 70;
                    if (!AssignProcessToJobObject(job, child.Handle))
                    {
                        child.Kill();
                        child.WaitForExit();
                        return 70;
                    }
                    child.WaitForExit();
                    return child.ExitCode;
                }
            }
            finally
            {
                CloseHandle(job);
            }
        }
        catch
        {
            return 70;
        }
    }
}
