// A small Windows GUI host. Python and Qt run inside Toshy.exe, not a child pythonw.exe.
using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

[assembly: AssemblyTitle("Toshy")]
[assembly: AssemblyProduct("Toshy Agent Studio")]
[assembly: AssemblyDescription("Toshy Agent Studio")]
[assembly: AssemblyCompany("supertoshibaz")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class ToshyHost {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr LoadLibraryEx(string name, IntPtr file, uint flags);
    [DllImport("kernel32.dll", CharSet=CharSet.Ansi, SetLastError=true)]
    static extern IntPtr GetProcAddress(IntPtr library, string name);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr AddDllDirectory(string directory);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    static extern int MessageBox(IntPtr window, string text, string title, uint flags);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate int PyMain(int argc, IntPtr argv);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate void SetProgramName(IntPtr name);

    static void Log(string path, string text) {
        using (FileStream stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
        using (StreamWriter writer = new StreamWriter(stream, new UTF8Encoding(false)))
            writer.WriteLine(DateTime.Now.ToString("s") + " " + text);
    }

    [STAThread]
    static int Main(string[] args) {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string root = Path.GetFullPath(Path.Combine(directory, ".."));
        string log = Path.Combine(root, "runtime", "startup.log");
        try {
            Directory.CreateDirectory(Path.GetDirectoryName(log));
            Log(log, "Starting Toshy.exe");
            string[] config = File.ReadAllLines(Path.Combine(directory, "python-host.cfg"), Encoding.UTF8);
            if (config.Length != 4) throw new InvalidOperationException("Invalid Python host configuration. Run install.ps1 again.");
            root = config[0];
            string dll = config[1], python = config[2], startup = config[3];
            if (!File.Exists(dll) || !File.Exists(python) || !File.Exists(startup))
                throw new FileNotFoundException("A Toshy runtime file is missing. Run install.ps1 again.");
            Environment.SetEnvironmentVariable("TOSHY_INSTALL_ROOT", root);
            Environment.SetEnvironmentVariable("TOSHY_STARTUP_LOG", log);
            Environment.SetEnvironmentVariable("TOSHY_WINDOWS_HOST_PROCESS", System.Diagnostics.Process.GetCurrentProcess().Id.ToString());
            Environment.SetEnvironmentVariable("PYTHONNOUSERSITE", "1");
            Environment.SetEnvironmentVariable("PYTHONHOME", null);
            Environment.SetEnvironmentVariable("PYTHONPATH", null);
            // Explicit absolute paths and safe DLL search. Keep Python's own DLL dependencies nearby.
            AddDllDirectory(Path.GetDirectoryName(dll));
            string dlls = Path.Combine(Path.GetDirectoryName(dll), "DLLs");
            if (Directory.Exists(dlls)) AddDllDirectory(dlls);
            IntPtr library = LoadLibraryEx(dll, IntPtr.Zero, 0x00000100 | 0x00001000);
            if (library == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "Could not load the private Python runtime: " + dll);
            IntPtr entry = GetProcAddress(library, "Py_Main");
            IntPtr setName = GetProcAddress(library, "Py_SetProgramName");
            if (entry == IntPtr.Zero || setName == IntPtr.Zero) throw new InvalidOperationException("The Python runtime does not provide its startup API.");
            PyMain run = (PyMain)Marshal.GetDelegateForFunctionPointer(entry, typeof(PyMain));
            SetProgramName setProgram = (SetProgramName)Marshal.GetDelegateForFunctionPointer(setName, typeof(SetProgramName));
            string[] command = new string[args.Length + 3];
            command[0] = python; command[1] = "-u"; command[2] = startup;
            Array.Copy(args, 0, command, 3, args.Length);
            IntPtr[] strings = new IntPtr[command.Length];
            IntPtr argv = Marshal.AllocHGlobal(IntPtr.Size * command.Length);
            IntPtr programName = Marshal.StringToHGlobalUni(python);
            int result;
            try {
                setProgram(programName);
                for (int i=0; i<command.Length; i++) strings[i] = Marshal.StringToHGlobalUni(command[i]);
                Marshal.Copy(strings, 0, argv, strings.Length);
                result = run(command.Length, argv);
            } finally {
                foreach (IntPtr value in strings) if (value != IntPtr.Zero) Marshal.FreeHGlobal(value);
                Marshal.FreeHGlobal(argv);
                Marshal.FreeHGlobal(programName);
            }
            Log(log, "Toshy exited with code " + result);
            if (result != 0) MessageBox(IntPtr.Zero, "Toshy could not start or exited with an error.\n\nDetails: " + log, "Toshy startup error", 0x10);
            return result;
        } catch (Exception error) {
            try { Log(log, error.ToString()); } catch { }
            MessageBox(IntPtr.Zero, error.Message + "\n\nDetails: " + log, "Toshy startup error", 0x10);
            return 2;
        }
    }
}
