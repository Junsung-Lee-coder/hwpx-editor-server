Set-StrictMode -Version Latest

# All values that cross the native/receipt boundary are deliberately bounded.
# These limits protect preflight from pathological native output or malformed
# source metadata while retaining enough context for operator diagnosis.
$script:MaxNativeOutputChars = 65536
$script:MaxNativeOutputBytes = $script:MaxNativeOutputChars * 4
$script:DefaultNativeTimeoutSeconds = 600
$script:MaxProcessCommandLineChars = 4096
$script:MaxReceiptBytes = 4194304
$script:MaxSnapshotBytes = 4194304
$script:MaxManifestBytes = 8388608
$script:MaxManifestEntries = 2048
$script:MaxTaskXmlChars = 262144
$script:MaxSnapshotTasks = 64
$script:MaxSnapshotProcesses = 256
$script:InstallLockTimeoutSeconds = 120
$script:DefaultApiPort = 8765
$script:DefaultTaskPath = '\'
$script:ProhibitedPrivateBasenames = @(
    'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'authorized_keys', 'known_hosts',
    'credentials', 'credentials.json', 'secrets', 'secrets.json', 'secret.json',
    'token', 'tokens.json', 'cookie', 'cookies.json', 'private_key', 'private-key',
    'service-account.json', '.env', '.coverage', '.ds_store', 'thumbs.db'
)
$script:ProhibitedPrivatePrefixes = @('config.local.', 'config.override.', 'local.settings.')
$script:ProhibitedPrivateStemMarkers = @('credential', 'secret', 'password', 'passwd', 'token', 'cookie')
$script:ProhibitedRuntimeDirectories = @(
    '.git', '.venv', '.venv313', 'venv', '__pycache__', '.mypy_cache', '.pytest_cache',
    '.ruff_cache', '.tox', '.vscode', '.idea', 'htmlcov', '.egg-info', 'spool', 'receipts', 'fixtures',
    'uploads', 'output', 'logs', 'cache', 'backups', 'proofs', 'evidence', 'runtime',
    'queue', 'documents', 'customer', 'projects', 'sessions', 'ocr', 'renders', 'env',
    'source-bundle', 'artifacts', 'archives', 'staging', 'temp', 'tmp', 'build', 'dist'
)

function Test-ProhibitedPrivateSourceMember {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    $parts = @($RelativePath.Replace('/', '\').Split([char]92) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    foreach ($part in $parts) {
        $folded = $part.ToLowerInvariant()
        if ($folded.StartsWith('.env') -or $script:ProhibitedPrivateBasenames -contains $folded -or
            @($script:ProhibitedPrivatePrefixes | Where-Object { $folded.StartsWith($_) }).Count -gt 0 -or
            @($script:ProhibitedPrivateStemMarkers | Where-Object { $folded.Contains($_) }).Count -gt 0) {
            return $true
        }
    }
    return $false
}

function Test-ProhibitedSourceMember {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    $parts = @($RelativePath.Replace('/', '\').Split([char]92) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($parts.Count -eq 0) { return $true }
    foreach ($part in $parts) {
        $folded = $part.ToLowerInvariant()
        if ($script:ProhibitedRuntimeDirectories -contains $folded -or $folded.StartsWith('.hwpx-install') -or
            (Test-ProhibitedPrivateSourceMember -RelativePath $RelativePath)) { return $true }
        $extension = [IO.Path]::GetExtension($folded)
        if ($extension -in @('.hwp', '.hwpx', '.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp', '.pem', '.key', '.p12', '.pfx', '.crt', '.cer', '.der', '.kdbx', '.pyc', '.pyo', '.pyd', '.pid', '.db', '.sqlite', '.sqlite3', '.log', '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z')) { return $true }
    }
    if ($parts.Count -eq 1 -and ($parts[0].ToLowerInvariant() -in @('manifest.json', 'source-manifest.json', 'source_bundle_manifest.json') -or $parts[0].ToLowerInvariant().EndsWith('.manifest.json'))) { return $true }
    return $false
}

# Windows path checks need a filesystem identity stronger than a pathname or
# same-byte inventory.  The Win32 file index remains stable while a directory
# is moved, and the handle is opened with delete sharing disabled by the
# caller's verification boundary.
if ($null -eq ('HwpxInstallNative.Identity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;

namespace HwpxInstallNative {
    public static class Identity {
        private const uint GENERIC_READ = 0x80000000;
        private const uint FILE_SHARE_READ = 0x00000001;
        private const uint FILE_SHARE_WRITE = 0x00000002;
        private const uint FILE_SHARE_DELETE = 0x00000004;
        private const uint OPEN_EXISTING = 3;
        private const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
        private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

        [StructLayout(LayoutKind.Sequential)]
        private struct BY_HANDLE_FILE_INFORMATION {
            public uint FileAttributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateFile(
            string name, uint access, uint share, IntPtr security, uint creation,
            uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            IntPtr handle, out BY_HANDLE_FILE_INFORMATION information);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFileInformationByHandle(
            IntPtr handle, int informationClass, IntPtr fileInformation, uint bufferSize);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFileAttributes(string name);

        private const uint DELETE_ACCESS = 0x00010000;
        private const uint FILE_ATTRIBUTE_DIRECTORY = 0x00000010;
        private const uint FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400;
        private const int FileRenameInfo = 3;
        private const int FileDispositionInfo = 4;
        private const int FileDispositionInfoEx = 21;
        private const uint FILE_DISPOSITION_FLAG_DELETE = 0x00000001;
        private const uint FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002;

        [StructLayout(LayoutKind.Sequential)]
        private struct FILE_DISPOSITION_INFO {
            public byte DeleteFile;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct FILE_DISPOSITION_INFO_EX {
            public uint Flags;
        }

        private static string GetIdentity(IntPtr handle) {
            BY_HANDLE_FILE_INFORMATION info;
            if (!GetFileInformationByHandle(handle, out info))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "GetFileInformationByHandle failed");
            ulong index = ((ulong)info.FileIndexHigh << 32) | info.FileIndexLow;
            return info.VolumeSerialNumber.ToString("X8") + ":" + index.ToString("X16");
        }

        private static IntPtr OpenMutationHandle(string path) {
            IntPtr handle = CreateFile(
                path, GENERIC_READ | DELETE_ACCESS,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                IntPtr.Zero, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, IntPtr.Zero);
            if (handle == INVALID_HANDLE_VALUE)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateFile failed for identity-bound mutation");
            return handle;
        }

        private static void MarkDeleted(IntPtr handle) {
            FILE_DISPOSITION_INFO_EX extended = new FILE_DISPOSITION_INFO_EX();
            extended.Flags = FILE_DISPOSITION_FLAG_DELETE | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS;
            IntPtr extendedBuffer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FILE_DISPOSITION_INFO_EX)));
            try {
                Marshal.StructureToPtr(extended, extendedBuffer, false);
                if (SetFileInformationByHandle(
                    handle, FileDispositionInfoEx, extendedBuffer,
                    (uint)Marshal.SizeOf(typeof(FILE_DISPOSITION_INFO_EX)))) return;
            }
            finally { Marshal.FreeHGlobal(extendedBuffer); }

            FILE_DISPOSITION_INFO legacy = new FILE_DISPOSITION_INFO();
            legacy.DeleteFile = 1;
            IntPtr legacyBuffer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FILE_DISPOSITION_INFO)));
            try {
                Marshal.StructureToPtr(legacy, legacyBuffer, false);
                if (!SetFileInformationByHandle(
                    handle, FileDispositionInfo, legacyBuffer,
                    (uint)Marshal.SizeOf(typeof(FILE_DISPOSITION_INFO))) )
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "SetFileInformationByHandle delete failed");
            }
            finally { Marshal.FreeHGlobal(legacyBuffer); }
        }

        private static void DeletePathContents(string path) {
            string[] children = System.IO.Directory.GetFileSystemEntries(path);
            foreach (string child in children) {
                uint attributes = GetFileAttributes(child);
                if (attributes == 0xFFFFFFFF)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "GetFileAttributes failed during identity-bound delete");
                if ((attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0)
                    throw new IOException("Reparse point encountered during identity-bound delete: " + child);
                IntPtr childHandle = OpenMutationHandle(child);
                try {
                    string childIdentity = GetIdentity(childHandle);
                    if ((attributes & FILE_ATTRIBUTE_DIRECTORY) != 0) {
                        DeletePathContents(child);
                    }
                    MarkDeleted(childHandle);
                    // Read the identity once more while the child handle is
                    // still held; a replacement cannot be silently deleted.
                    if (GetIdentity(childHandle) != childIdentity)
                        throw new IOException("Child identity changed during identity-bound delete: " + child);
                }
                finally { CloseHandle(childHandle); }
            }
        }

        public static void DeletePathIfIdentity(string path, string expectedIdentity) {
            IntPtr handle = OpenMutationHandle(path);
            try {
                string actualIdentity = GetIdentity(handle);
                if (actualIdentity != expectedIdentity)
                    throw new IOException("Path identity changed before identity-bound delete: " + path);
                uint attributes = GetFileAttributes(path);
                if (attributes == 0xFFFFFFFF)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "GetFileAttributes failed before identity-bound delete");
                if ((attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0)
                    throw new IOException("Reparse point is not allowed for identity-bound delete: " + path);
                if ((attributes & FILE_ATTRIBUTE_DIRECTORY) != 0)
                    DeletePathContents(path);
                MarkDeleted(handle);
                if (GetIdentity(handle) != actualIdentity)
                    throw new IOException("Path identity changed during identity-bound delete: " + path);
            }
            finally { CloseHandle(handle); }
        }

        public static void MovePathIfIdentity(string source, string expectedIdentity, string destination) {
            IntPtr handle = OpenMutationHandle(source);
            try {
                string actualIdentity = GetIdentity(handle);
                if (actualIdentity != expectedIdentity)
                    throw new IOException("Path identity changed before identity-bound move: " + source);
                uint sourceAttributes = GetFileAttributes(source);
                if (sourceAttributes == 0xFFFFFFFF)
                    throw new IOException("Source pathname no longer names the identity-bound object: " + source);
                uint destinationAttributes = GetFileAttributes(destination);
                if (destinationAttributes != 0xFFFFFFFF)
                    throw new IOException("Identity-bound move destination already exists: " + destination);
                int nameOffset = IntPtr.Size == 8 ? 20 : 12;
                byte[] nameBytes = System.Text.Encoding.Unicode.GetBytes(destination);
                int renameBufferSize = nameOffset + nameBytes.Length + 2;
                IntPtr renameBuffer = Marshal.AllocHGlobal(renameBufferSize);
                try {
                    for (int index = 0; index < renameBufferSize; index++) Marshal.WriteByte(renameBuffer, index, 0);
                    Marshal.WriteByte(renameBuffer, 0, 0);
                    Marshal.WriteIntPtr(renameBuffer, IntPtr.Size == 8 ? 8 : 4, IntPtr.Zero);
                    Marshal.WriteInt32(renameBuffer, IntPtr.Size == 8 ? 16 : 8, nameBytes.Length);
                    Marshal.Copy(nameBytes, 0, IntPtr.Add(renameBuffer, nameOffset), nameBytes.Length);
                    if (!SetFileInformationByHandle(handle, FileRenameInfo, renameBuffer, (uint)renameBufferSize))
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "SetFileInformationByHandle rename failed");
                }
                finally { Marshal.FreeHGlobal(renameBuffer); }
                if (GetFileAttributes(destination) == 0xFFFFFFFF)
                    throw new IOException("Identity-bound move destination was not visible after rename: " + destination);
                if (GetIdentity(handle) != actualIdentity)
                    throw new IOException("Identity-bound move handle identity changed during rename: " + source);
            }
            finally { CloseHandle(handle); }
        }

        public static string GetPathIdentity(string path) {
            IntPtr handle = CreateFile(
                path, GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                IntPtr.Zero, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, IntPtr.Zero);
            if (handle == INVALID_HANDLE_VALUE)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateFile failed for path identity");
            try {
                return GetIdentity(handle);
            }
            finally {
                CloseHandle(handle);
            }
        }
    }

    public static class Job {
        private const uint PROCESS_TERMINATE = 0x0001;
        private const uint PROCESS_SET_QUOTA = 0x0100;
        private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;
        private const int JobObjectExtendedLimitInformation = 9;
        private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
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
        private struct IO_COUNTERS {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job, int informationClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION information, uint length);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inheritHandle, uint processId);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public static IntPtr CreateKillOnCloseJob() {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero || job == INVALID_HANDLE_VALUE)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
            try {
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION information =
                    new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if (!SetInformationJobObject(
                    job, JobObjectExtendedLimitInformation, ref information,
                    (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION))))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "SetInformationJobObject failed");
                return job;
            }
            catch {
                CloseHandle(job);
                throw;
            }
        }

        public static void AssignProcess(IntPtr job, int processId) {
            if (job == IntPtr.Zero || job == INVALID_HANDLE_VALUE || processId <= 0)
                throw new ArgumentException("A valid job handle and process id are required");
            IntPtr process = OpenProcess(
                PROCESS_TERMINATE | PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION,
                false, (uint)processId);
            if (process == IntPtr.Zero || process == INVALID_HANDLE_VALUE)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "OpenProcess failed for job assignment");
            try {
                if (!AssignProcessToJobObject(job, process))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
            }
            finally { CloseHandle(process); }
        }

        public static void Terminate(IntPtr job) {
            if (job != IntPtr.Zero && job != INVALID_HANDLE_VALUE &&
                !TerminateJobObject(job, 0xC000013A))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "TerminateJobObject failed");
        }

        public static void Close(IntPtr job) {
            if (job != IntPtr.Zero && job != INVALID_HANDLE_VALUE) CloseHandle(job);
        }
    }
}
'@
}

if ($null -eq ('HwpxInstallNative.SuspendedProcess' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace HwpxInstallNative {
    public sealed class SuspendedProcess : IDisposable {
        private const uint CREATE_SUSPENDED = 0x00000004;
        private const uint CREATE_NO_WINDOW = 0x08000000;
        private const uint STARTF_USESTDHANDLES = 0x00000100;
        private const uint HANDLE_FLAG_INHERIT = 0x00000001;
        private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

        [StructLayout(LayoutKind.Sequential)]
        private struct SECURITY_ATTRIBUTES {
            public int Length;
            public IntPtr SecurityDescriptor;
            public int InheritHandle;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO {
            public int cb;
            public string Reserved;
            public string Desktop;
            public string Title;
            public int X;
            public int Y;
            public int XSize;
            public int YSize;
            public int XCountChars;
            public int YCountChars;
            public int FillAttribute;
            public uint Flags;
            public short ShowWindow;
            public short Reserved2;
            public IntPtr Reserved2Ptr;
            public IntPtr StdInput;
            public IntPtr StdOutput;
            public IntPtr StdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION {
            public IntPtr ProcessHandle;
            public IntPtr ThreadHandle;
            public int ProcessId;
            public int ThreadId;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CreatePipe(out IntPtr readPipe, out IntPtr writePipe, ref SECURITY_ATTRIBUTES attributes, int size);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcess(
            string applicationName, StringBuilder commandLine, IntPtr processAttributes,
            IntPtr threadAttributes, bool inheritHandles, uint creationFlags,
            IntPtr environment, string currentDirectory, ref STARTUPINFO startupInfo,
            out PROCESS_INFORMATION processInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        private IntPtr _processHandle;
        private IntPtr _threadHandle;
        private IntPtr _stdoutRead;
        private IntPtr _stderrRead;

        private SuspendedProcess(PROCESS_INFORMATION information, IntPtr stdoutRead, IntPtr stderrRead) {
            _processHandle = information.ProcessHandle;
            _threadHandle = information.ThreadHandle;
            _stdoutRead = stdoutRead;
            _stderrRead = stderrRead;
            ProcessId = information.ProcessId;
            ManagedProcess = Process.GetProcessById(ProcessId);
        }

        public int ProcessId { get; private set; }
        public int Id { get { return ProcessId; } }
        public Process ManagedProcess { get; private set; }
        public bool HasExited { get { return ManagedProcess.HasExited; } }
        public int ExitCode {
            get {
                uint exitCode;
                if (!GetExitCodeProcess(_processHandle, out exitCode))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "GetExitCodeProcess failed");
                return unchecked((int)exitCode);
            }
        }

        public static SuspendedProcess Create(string applicationName, string commandLine, string workingDirectory) {
            SECURITY_ATTRIBUTES attributes = new SECURITY_ATTRIBUTES();
            attributes.Length = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
            attributes.InheritHandle = 1;
            IntPtr stdoutRead = IntPtr.Zero, stdoutWrite = IntPtr.Zero;
            IntPtr stderrRead = IntPtr.Zero, stderrWrite = IntPtr.Zero;
            PROCESS_INFORMATION information = new PROCESS_INFORMATION();
            try {
                if (!CreatePipe(out stdoutRead, out stdoutWrite, ref attributes, 0))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "CreatePipe stdout failed");
                if (!CreatePipe(out stderrRead, out stderrWrite, ref attributes, 0))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "CreatePipe stderr failed");
                if (!SetHandleInformation(stdoutRead, HANDLE_FLAG_INHERIT, 0) ||
                    !SetHandleInformation(stderrRead, HANDLE_FLAG_INHERIT, 0))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "SetHandleInformation failed");
                STARTUPINFO startup = new STARTUPINFO();
                startup.cb = Marshal.SizeOf(typeof(STARTUPINFO));
                startup.Flags = STARTF_USESTDHANDLES;
                startup.StdOutput = stdoutWrite;
                startup.StdError = stderrWrite;
                StringBuilder mutableCommandLine = new StringBuilder(commandLine ?? "");
                if (!CreateProcess(applicationName, mutableCommandLine, IntPtr.Zero, IntPtr.Zero, true,
                    CREATE_SUSPENDED | CREATE_NO_WINDOW, IntPtr.Zero, workingDirectory, ref startup, out information))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateProcess suspended failed");
                CloseHandle(stdoutWrite); stdoutWrite = IntPtr.Zero;
                CloseHandle(stderrWrite); stderrWrite = IntPtr.Zero;
                return new SuspendedProcess(information, stdoutRead, stderrRead);
            }
            catch {
                // CreateProcess returns a live suspended process before the
                // wrapper has finished constructing its managed identity.
                // If that construction fails, closing the handle alone leaks
                // a permanently suspended process outside the owned job.
                if (information.ProcessHandle != IntPtr.Zero) {
                    try { TerminateProcess(information.ProcessHandle, 0xC000013A); } catch { }
                }
                if (information.ThreadHandle != IntPtr.Zero) CloseHandle(information.ThreadHandle);
                if (information.ProcessHandle != IntPtr.Zero) CloseHandle(information.ProcessHandle);
                if (stdoutRead != IntPtr.Zero) CloseHandle(stdoutRead);
                if (stdoutWrite != IntPtr.Zero) CloseHandle(stdoutWrite);
                if (stderrRead != IntPtr.Zero) CloseHandle(stderrRead);
                if (stderrWrite != IntPtr.Zero) CloseHandle(stderrWrite);
                throw;
            }
        }

        public void Resume() {
            if (_threadHandle == IntPtr.Zero) throw new InvalidOperationException("Suspended process thread handle is unavailable");
            uint result = ResumeThread(_threadHandle);
            if (result == UInt32.MaxValue)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "ResumeThread failed");
            CloseHandle(_threadHandle);
            _threadHandle = IntPtr.Zero;
        }

        public Stream OpenStdoutStream() {
            if (_stdoutRead == IntPtr.Zero) throw new InvalidOperationException("stdout pipe is unavailable");
            IntPtr handle = _stdoutRead; _stdoutRead = IntPtr.Zero;
            return new FileStream(new SafeFileHandle(handle, true), FileAccess.Read, 8192, false);
        }

        public Stream OpenStderrStream() {
            if (_stderrRead == IntPtr.Zero) throw new InvalidOperationException("stderr pipe is unavailable");
            IntPtr handle = _stderrRead; _stderrRead = IntPtr.Zero;
            return new FileStream(new SafeFileHandle(handle, true), FileAccess.Read, 8192, false);
        }

        public bool WaitForExit(int milliseconds) { return ManagedProcess.WaitForExit(milliseconds); }
        public void WaitForExit() { ManagedProcess.WaitForExit(); }
        public void Kill() {
            if (!HasExited && !TerminateProcess(_processHandle, 0xC000013A))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "TerminateProcess failed");
        }

        public void Dispose() {
            if (_stdoutRead != IntPtr.Zero) { CloseHandle(_stdoutRead); _stdoutRead = IntPtr.Zero; }
            if (_stderrRead != IntPtr.Zero) { CloseHandle(_stderrRead); _stderrRead = IntPtr.Zero; }
            if (_threadHandle != IntPtr.Zero) { CloseHandle(_threadHandle); _threadHandle = IntPtr.Zero; }
            if (_processHandle != IntPtr.Zero) { CloseHandle(_processHandle); _processHandle = IntPtr.Zero; }
            if (ManagedProcess != null) { ManagedProcess.Dispose(); ManagedProcess = null; }
        }
    }
}
'@
}

if ($null -eq ('HwpxInstallNative.ProcessAuthority' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace HwpxInstallNative {
    public static class ProcessAuthority {
        private const uint PROCESS_TERMINATE = 0x0001;
        private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        private const uint STILL_ACTIVE = 259;
        private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

        [StructLayout(LayoutKind.Sequential)]
        private struct FILETIME {
            public uint LowDateTime;
            public uint HighDateTime;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inheritHandle, uint processId);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetProcessTimes(
            IntPtr process, out FILETIME creation, out FILETIME exit,
            out FILETIME kernel, out FILETIME user);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public static IntPtr Open(int processId) {
            if (processId <= 0) throw new ArgumentException("A positive process id is required", "processId");
            IntPtr handle = OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, false, (uint)processId);
            if (handle == IntPtr.Zero || handle == INVALID_HANDLE_VALUE)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "OpenProcess failed for termination authority");
            return handle;
        }

        public static IntPtr OpenForQuery(int processId) {
            if (processId <= 0) throw new ArgumentException("A positive process id is required", "processId");
            IntPtr handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, (uint)processId);
            if (handle == IntPtr.Zero || handle == INVALID_HANDLE_VALUE)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "OpenProcess failed for process identity query");
            return handle;
        }

        public static string GetStartIdentity(IntPtr handle) {
            FILETIME creation, exit, kernel, user;
            if (!GetProcessTimes(handle, out creation, out exit, out kernel, out user))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "GetProcessTimes failed");
            ulong ticks = ((ulong)creation.HighDateTime << 32) | creation.LowDateTime;
            return ticks.ToString("X16");
        }

        public static bool IsAlive(IntPtr handle) {
            uint exitCode;
            if (!GetExitCodeProcess(handle, out exitCode))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "GetExitCodeProcess failed");
            return exitCode == STILL_ACTIVE;
        }

        public static void Terminate(IntPtr handle) {
            if (handle == IntPtr.Zero || handle == INVALID_HANDLE_VALUE || !TerminateProcess(handle, 0xC000013A))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "TerminateProcess failed");
        }

        public static void Close(IntPtr handle) {
            if (handle != IntPtr.Zero && handle != INVALID_HANDLE_VALUE) CloseHandle(handle);
        }
    }
}
'@
}

function Limit-Text {
    param(
        [AllowNull()][object]$Value,
        [int]$MaxChars = $script:MaxProcessCommandLineChars
    )
    if ($MaxChars -lt 1) { throw 'MaxChars must be positive.' }
    $text = if ($null -eq $Value) { '' } else { [string]$Value }
    if ($text.Length -le $MaxChars) { return $text }
    return $text.Substring(0, $MaxChars)
}

function Read-BoundedText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$MaxChars = $script:MaxNativeOutputChars
    )
    if ($MaxChars -lt 1) { throw 'MaxChars must be positive.' }
    $stream = $null
    $reader = $null
    try {
        $stream = [System.IO.File]::Open(
            (Get-CanonicalPath -Path $Path -RequireExisting),
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8, $true, 4096)
        # Read one extra character so truncation is known without loading the
        # remainder of a potentially unbounded native output file.
        $buffer = New-Object char[] ($MaxChars + 1)
        $readCount = $reader.Read($buffer, 0, $buffer.Length)
        $capturedCount = [Math]::Min($readCount, $MaxChars)
        $text = if ($capturedCount -gt 0) { [System.String]::new($buffer, 0, $capturedCount) } else { '' }
        return [pscustomobject]@{
            text = [string]$text
            bytes = [int64]$stream.Length
            truncated = ($readCount -gt $MaxChars)
        }
    }
    finally {
        if ($null -ne $reader) { $reader.Dispose() }
        elseif ($null -ne $stream) { $stream.Dispose() }
    }
}

function Read-BoundedJsonObject {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int64]$MaxBytes = $script:MaxReceiptBytes
    )
    if ($MaxBytes -lt 1) { throw 'MaxBytes must be positive.' }
    $canonical = Get-CanonicalPath -Path $Path -RequireExisting
    $identityBefore = Get-PathObjectIdentity -Path $canonical -RequireExisting
    $stream = $null
    $bytes = $null
    try {
        $stream = [IO.File]::Open($canonical, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $length = [int64]$stream.Length
        if ($length -gt $MaxBytes) { throw "JSON object exceeds the bounded size limit: $canonical" }
        $bytes = New-Object byte[] ([int]$length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $readCount = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($readCount -le 0) { throw "JSON object read ended before the declared length: $canonical" }
            $offset += $readCount
        }
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { $hash = ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        $value = $text | ConvertFrom-Json
    }
    catch {
        throw "Bounded JSON read failed: $canonical ($($_.Exception.Message))"
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    Assert-PathObjectIdentity -Path $canonical -ExpectedIdentity $identityBefore | Out-Null
    return [pscustomobject]@{
        path = $canonical
        value = $value
        bytes = [int64]$bytes.Length
        sha256 = $hash
        object_identity = $identityBefore
    }
}

function Get-ConfiguredEnvValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$EnvPath,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ([string]::IsNullOrWhiteSpace($EnvPath) -or -not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name -notmatch '^HWP_[A-Za-z0-9_]+$') {
        throw "Invalid configuration variable name: $Name"
    }
    $capture = Read-BoundedText -Path $EnvPath -MaxChars $script:MaxNativeOutputChars
    if ($capture.truncated) {
        throw "Environment configuration exceeds the bounded size limit of $script:MaxNativeOutputChars characters: $EnvPath"
    }
    $pattern = '^\s*' + [regex]::Escape($Name) + '\s*=\s*(.*?)\s*(?:#.*)?$'
    foreach ($line in @([string]$capture.text -split "`r?`n")) {
        if ([string]$line -match $pattern) { return [string]$matches[1] }
    }
    return $null
}

function Get-ConfiguredApiPort {
    [CmdletBinding()]
    param([string]$EnvPath)
    if ([string]::IsNullOrWhiteSpace($EnvPath) -or -not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
        return $script:DefaultApiPort
    }
    $envCapture = Read-BoundedText -Path $EnvPath -MaxChars $script:MaxNativeOutputChars
    if ($envCapture.truncated) {
        throw "Environment configuration exceeds the bounded size limit of $script:MaxNativeOutputChars characters: $EnvPath"
    }
    $configured = $null
    foreach ($line in @([string]$envCapture.text -split "`r?`n")) {
        $candidate = [string]$line
        if ($candidate -match '^\s*HWP_API_PORT\s*=\s*([0-9]+)\s*(?:#.*)?$') {
            $configured = [int]$matches[1]
        }
    }
    if ($null -eq $configured) { return $script:DefaultApiPort }
    if ($configured -lt 1 -or $configured -gt 65535) {
        throw "HWP_API_PORT must be between 1 and 65535: $configured"
    }
    return [int]$configured
}

function Resolve-ApiPort {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Nullable[int]]$RequestedApiPort
    )
    $requested = $RequestedApiPort
    if ($null -ne $requested -and ($requested -lt 1 -or $requested -gt 65535)) {
        throw "ApiPort must be between 1 and 65535: $requested"
    }
    $envPath = Join-Path $InstallRoot '.env'
    $hasExistingEnv = Test-Path -LiteralPath $envPath -PathType Leaf
    $configured = if ($hasExistingEnv) { Get-ConfiguredApiPort -EnvPath $envPath } else { $script:DefaultApiPort }
    if ($null -ne $requested) {
        if ($hasExistingEnv -and $configured -ne [int]$requested) {
            throw "Requested ApiPort $requested conflicts with preserved HWP_API_PORT $configured in $envPath."
        }
        return [int]$requested
    }
    return [int]$configured
}

function Get-CanonicalPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [switch]$RequireExisting
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'Path must not be empty.'
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    $full = [System.IO.Path]::GetFullPath($expanded)
    $trimChars = [char[]]@([char]92, [char]47)
    $pathRoot = [System.IO.Path]::GetPathRoot($full)
    if (Test-Path -LiteralPath $full) {
        $resolved = Resolve-Path -LiteralPath $full -ErrorAction Stop
        $resolvedPath = [string]$resolved.Path
        if (-not [string]::IsNullOrWhiteSpace($pathRoot) -and $resolvedPath.Length -le $pathRoot.Length) {
            return $pathRoot
        }
        return $resolvedPath.TrimEnd($trimChars)
    }
    if ($RequireExisting) {
        throw "Path does not exist: $full"
    }
    if (-not [string]::IsNullOrWhiteSpace($pathRoot) -and $full.Length -le $pathRoot.Length) {
        return $pathRoot
    }
    return $full.TrimEnd($trimChars)
}

function Get-PathObjectIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$RequireExisting
    )
    $canonical = Get-CanonicalPath -Path $Path -RequireExisting:$RequireExisting
    if (-not (Test-Path -LiteralPath $canonical)) {
        if ($RequireExisting) { throw "Path does not exist for object identity: $Path" }
        return $null
    }
    Assert-NoReparsePath -Path $canonical | Out-Null
    try {
        return [HwpxInstallNative.Identity]::GetPathIdentity($canonical)
    }
    catch {
        throw "Could not capture stable filesystem object identity for ${canonical}: $($_.Exception.Message)"
    }
}

function Assert-PathObjectIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedIdentity
    )
    if ([string]::IsNullOrWhiteSpace($ExpectedIdentity)) {
        throw "Expected stable filesystem object identity is missing: $Path"
    }
    $actual = Get-PathObjectIdentity -Path $Path -RequireExisting
    if ([string]$actual -cne [string]$ExpectedIdentity) {
        throw "Filesystem object identity changed: expected $ExpectedIdentity, got $actual ($Path)"
    }
    return $actual
}

function Get-PathMutexName {
    param(
        [Parameter(Mandatory = $true)][string]$Key
    )
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Key)
        $digest = ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
    return ('Global' + [char]92 + 'HWPX-Install-' + $digest)
}

function Set-SecuredMutexAccess {
    param([Parameter(Mandatory = $true)][object]$Mutex)
    $security = New-Object System.Security.AccessControl.MutexSecurity
    $sidValues = @('S-1-5-18', 'S-1-5-32-544')
    try { $sidValues += [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value } catch { }
    if (-not [string]::IsNullOrWhiteSpace($env:HWPX_INSTALL_LOCK_SIDS)) {
        $sidValues += @($env:HWPX_INSTALL_LOCK_SIDS -split ',')
    }
    foreach ($sidValue in @($sidValues | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -match '^S-1-[0-9-]+$' } | Select-Object -Unique)) {
        $sid = New-Object System.Security.Principal.SecurityIdentifier($sidValue)
        $rule = New-Object System.Security.AccessControl.MutexAccessRule(
            $sid,
            [System.Security.AccessControl.MutexRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $security.AddAccessRule($rule)
    }
    $Mutex.SetAccessControl($security)
}

function Enter-PathMutex {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Purpose,
        [ValidateSet('root', 'task', 'port', 'role', 'receipt', 'lifecycle')][string]$LockKind = 'lifecycle',
        [ValidateRange(1, 600)][int]$TimeoutSeconds = $script:InstallLockTimeoutSeconds
    )
    $mutexName = Get-PathMutexName -Key $Key
    $createdNew = $false
    $mutex = New-Object System.Threading.Mutex($false, $mutexName, [ref]$createdNew)
    $acquired = $false
    try {
        Set-SecuredMutexAccess -Mutex $mutex
        try {
            $acquired = [bool]$mutex.WaitOne($TimeoutSeconds * 1000)
        }
        catch [System.Threading.AbandonedMutexException] {
            # The previous owner died while the transaction was active. The
            # mutex is acquired by this call; the caller still verifies every
            # filesystem preimage before mutating it.
            $acquired = $true
        }
        if (-not $acquired) {
            throw "Timed out acquiring the $Purpose lock after $TimeoutSeconds seconds."
        }
        return [pscustomobject]@{
            purpose = $Purpose
            lock_kind = $LockKind
            key = $Key
            mutex_name = $mutexName
            acquired_at_utc = [DateTime]::UtcNow.ToString('o')
            created_new = $createdNew
            mutex = $mutex
        }
    }
    catch {
        try { $mutex.Dispose() } catch { }
        throw
    }
}

function Exit-PathMutex {
    param([AllowNull()][object]$Lock)
    if ($null -eq $Lock) { return }
    $nestedLocks = Get-OptionalPropertyValue -Object $Lock -Name 'locks'
    if ($null -ne $nestedLocks) {
        $nestedLockArray = @($nestedLocks)
        for ($nestedIndex = $nestedLockArray.Count - 1; $nestedIndex -ge 0; $nestedIndex--) {
            Exit-PathMutex -Lock $nestedLockArray[$nestedIndex]
        }
        return
    }
    $mutex = Get-OptionalPropertyValue -Object $Lock -Name 'mutex'
    if ($null -eq $mutex) { return }
    try { $mutex.ReleaseMutex() } catch { }
    try { $mutex.Dispose() } catch { }
}

function Enter-MachineLifecycleLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [string[]]$TaskNames = @(),
        [Nullable[int]]$ApiPort,
        [string]$Role = 'installer',
        [switch]$SkipVerifierAdmissionHandoff,
        [ValidateRange(1, 600)][int]$TimeoutSeconds = $script:InstallLockTimeoutSeconds
    )
    $canonical = Get-CanonicalPath -Path $InstallRoot
    $parent = Split-Path -Parent $canonical
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        Assert-NoReparsePath -Path $parent | Out-Null
    }
    $requests = @(
        [pscustomobject]@{ key = 'lifecycle:machine'; purpose = 'MachineLifecycle'; lock_kind = 'lifecycle' },
        [pscustomobject]@{ key = 'root:' + $canonical.ToLowerInvariant(); purpose = 'InstallRoot'; lock_kind = 'root' },
        [pscustomobject]@{ key = 'role:' + ([string]$Role).ToLowerInvariant(); purpose = 'LifecycleRole'; lock_kind = 'role' }
    )
    # The installer must release the machine/root locks while its independent
    # verifier acquires them. A root-scoped handoff mutex remains held by every
    # non-verifier role across that gap so another writer/installer cannot enter
    # between the two lock owners. The verifier itself skips this guard because
    # the parent installer owns it for the duration of the handoff.
    if (-not $SkipVerifierAdmissionHandoff -and [string]$Role -ine 'verifier') {
        $requests += [pscustomobject]@{ key = 'handoff:' + $canonical.ToLowerInvariant(); purpose = 'VerifierAdmissionHandoff'; lock_kind = 'root' }
    }
    foreach ($taskName in @($TaskNames | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $requests += [pscustomobject]@{ key = 'task:' + ([string]$taskName).ToLowerInvariant(); purpose = 'ScheduledTask'; lock_kind = 'task' }
    }
    if ($null -ne $ApiPort) {
        $requests += [pscustomobject]@{ key = 'port:' + [int]$ApiPort; purpose = 'ApiPort'; lock_kind = 'port' }
    }
    $locks = @()
    try {
        # All callers acquire the same set in lexical order, preventing a
        # root/task or port/role lock inversion between installer and writer.
        foreach ($request in @($requests | Sort-Object key)) {
            $locks += Enter-PathMutex -Key $request.key -Purpose $request.purpose -LockKind $request.lock_kind -TimeoutSeconds $TimeoutSeconds
        }
        return [pscustomobject]@{
            purpose = 'InstallLifecycle'
            canonical_root = $canonical
            role = $Role
            api_port = $ApiPort
            task_names = @($TaskNames)
            lock_kind = 'lifecycle'
            locks = $locks
            acquired_at_utc = [DateTime]::UtcNow.ToString('o')
        }
    }
    catch {
        $lockArray = @($locks)
        for ($lockIndex = $lockArray.Count - 1; $lockIndex -ge 0; $lockIndex--) {
            Exit-PathMutex -Lock $lockArray[$lockIndex]
        }
        throw
    }
}

function Add-MachineLifecycleLockScope {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Lock,
        [string[]]$TaskNames = @(),
        [Nullable[int]]$ApiPort,
        [ValidateRange(1, 600)][int]$TimeoutSeconds = $script:InstallLockTimeoutSeconds
    )
    $nestedLocks = @(Get-OptionalPropertyValue -Object $Lock -Name 'locks')
    if ($nestedLocks.Count -eq 0 -or @($nestedLocks | Where-Object { $_.key -eq 'lifecycle:machine' }).Count -ne 1) {
        throw 'Machine lifecycle lock scope is not held; refusing to extend serialization.'
    }
    $existingKeys = @($nestedLocks | ForEach-Object { [string]$_.key })
    $requests = @()
    foreach ($taskName in @($TaskNames | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $requests += [pscustomobject]@{ key = 'task:' + ([string]$taskName).ToLowerInvariant(); purpose = 'ScheduledTask'; lock_kind = 'task' }
    }
    if ($null -ne $ApiPort) {
        $requests += [pscustomobject]@{ key = 'port:' + [int]$ApiPort; purpose = 'ApiPort'; lock_kind = 'port' }
    }
    foreach ($request in @($requests | Sort-Object key)) {
        if ($existingKeys -contains $request.key) { continue }
        $nestedLocks += Enter-PathMutex -Key $request.key -Purpose $request.purpose -LockKind $request.lock_kind -TimeoutSeconds $TimeoutSeconds
        $existingKeys += $request.key
    }
    $Lock.locks = $nestedLocks
    return $Lock
}

function Enter-InstallLifecycleLock {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [string[]]$TaskNames = @(),
        [Nullable[int]]$ApiPort,
        [string]$Role = 'installer',
        [switch]$SkipVerifierAdmissionHandoff,
        [ValidateRange(1, 600)][int]$TimeoutSeconds = $script:InstallLockTimeoutSeconds
    )
    return Enter-MachineLifecycleLock -InstallRoot $InstallRoot -TaskNames $TaskNames -ApiPort $ApiPort -Role $Role -SkipVerifierAdmissionHandoff:$SkipVerifierAdmissionHandoff -TimeoutSeconds $TimeoutSeconds
}

function Exit-InstallLifecycleLock {
    param([AllowNull()][object]$Lock)
    Exit-PathMutex -Lock $Lock
}

function Enter-InstallRootLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [ValidateRange(1, 600)][int]$TimeoutSeconds = $script:InstallLockTimeoutSeconds
    )
    $canonical = Get-CanonicalPath -Path $InstallRoot
    $parent = Split-Path -Parent $canonical
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        Assert-NoReparsePath -Path $parent | Out-Null
    }
    return Enter-MachineLifecycleLock -InstallRoot $canonical -Role 'installer' -TimeoutSeconds $TimeoutSeconds
}

function Enter-ReceiptPathLock {
    param(
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [ValidateRange(1, 600)][int]$TimeoutSeconds = $script:InstallLockTimeoutSeconds
    )
    $canonical = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($ReceiptPath))
    return Enter-PathMutex -Key ("receipt:" + $canonical.ToLowerInvariant()) -Purpose 'receipt' -LockKind 'receipt' -TimeoutSeconds $TimeoutSeconds
}

function Assert-ReceiptPathAdmission {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][string]$InstallRoot
    )
    $expanded = [Environment]::ExpandEnvironmentVariables($ReceiptPath)
    if ([string]::IsNullOrWhiteSpace($expanded) -or $expanded.IndexOf([char]0) -ge 0) {
        throw 'ReceiptPath is empty or contains a NUL character.'
    }
    $candidate = [System.IO.Path]::GetFullPath($expanded)
    $parent = Split-Path -Parent $candidate
    if ([string]::IsNullOrWhiteSpace($parent)) { throw 'ReceiptPath must have a parent directory.' }
    Assert-NoReparsePath -Path $parent | Out-Null
    if (Test-Path -LiteralPath $candidate) {
        $candidateItem = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
        if (($candidateItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'ReceiptPath must not be a reparse point.'
        }
    }
    $canonicalInstall = Get-CanonicalPath -Path $InstallRoot
    $canonical = Get-CanonicalPath -Path $candidate
    if (Test-CanonicalPathWithinRoot -Path $canonical -Root $canonicalInstall) {
        throw 'ReceiptPath must be outside InstallRoot.'
    }
    if (Test-Path -LiteralPath $canonical) {
        $item = Get-Item -LiteralPath $canonical -Force -ErrorAction Stop
        if ($item.PSIsContainer) { throw 'ReceiptPath must identify a file, not a directory.' }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'ReceiptPath must not be a reparse point.'
        }
        $preimageIdentity = Get-PathObjectIdentity -Path $canonical -RequireExisting
        $preimageItem = Get-Item -LiteralPath $canonical -Force -ErrorAction Stop
        $preimageHash = Get-Sha256Hex -Path $canonical
        Assert-PathObjectIdentity -Path $canonical -ExpectedIdentity $preimageIdentity | Out-Null
        return [pscustomobject]@{
            path = $canonical
            exists = $true
            size = [int64]$preimageItem.Length
            sha256 = $preimageHash
            object_identity = $preimageIdentity
        }
    }
    return [pscustomobject]@{
        path = $canonical
        exists = $false
        size = $null
        sha256 = $null
        object_identity = $null
    }
}

function Get-InstallTransactionJournalPath {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $canonical = Get-CanonicalPath -Path $InstallRoot
    $base = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if ([string]::IsNullOrWhiteSpace($base)) { $base = [System.IO.Path]::GetTempPath() }
    $directory = Join-Path $base 'HWPX\transactions'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    Assert-NoReparsePath -Path $directory | Out-Null
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = ([System.BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($canonical.ToLowerInvariant())))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
    return Join-Path $directory ('install-' + $digest + '.journal.json')
}

function Write-StableTransactionJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )
    $target = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path))
    $parent = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Assert-NoReparsePath -Path $parent | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $payload = ([string]($Value | ConvertTo-Json -Depth 30)) + [Environment]::NewLine
    $bytes = $encoding.GetBytes($payload)
    if ($bytes.Length -gt $script:MaxSnapshotBytes) {
        throw "Install transaction journal exceeds the bounded size limit: $target"
    }
    $journalLock = Enter-PathMutex -Key ('journal:' + $target.ToLowerInvariant()) -Purpose 'transaction-journal' -LockKind 'lifecycle'
    $temporaryPath = Join-Path $parent ('.journal-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backupPath = Join-Path $parent ('.journal-backup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backupHoldPath = $backupPath + '.HOLD'
    $stream = $null
    $readbackValidated = $false
    try {
        $stream = [System.IO.File]::Open($temporaryPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::Read)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            Assert-NoReparsePath -Path $target | Out-Null
            # Windows PowerShell/.NET Framework rejects a null backup path;
            # retain the prior journal explicitly until the new bytes have
            # been flushed and read back successfully.
            [System.IO.File]::Replace($temporaryPath, $target, $backupPath)
        }
        else {
            [System.IO.File]::Move($temporaryPath, $target)
        }
        Assert-NoReparsePath -Path $target | Out-Null
        $stableStream = $null
        try {
            $stableStream = [System.IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::Read)
            $stableStream.Flush($true)
        }
        finally {
            if ($null -ne $stableStream) { $stableStream.Dispose() }
        }
        $readback = [System.IO.File]::ReadAllText($target, $encoding)
        if ($readback -cne $payload) { throw "Install transaction journal readback differed: $target" }
        $readbackValidated = $true
        return Get-CanonicalPath -Path $target -RequireExisting
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) { Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            if ($readbackValidated) {
                Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
            }
            else {
                # If replacement or readback failed, restore the last known
                # good journal and retain uncertain new bytes as HOLD evidence.
                try {
                    if (Test-Path -LiteralPath $target -PathType Leaf) {
                        [System.IO.File]::Replace($backupPath, $target, $backupHoldPath)
                    }
                    else {
                        [System.IO.File]::Move($backupPath, $target)
                    }
                }
                catch {
                    # Keep the backup at its stable path when restoration is
                    # uncertain; stale-run recovery must fail closed rather
                    # than discard the only known-good journal.
                }
            }
        }
        Exit-PathMutex -Lock $journalLock
    }
}

function Read-StableTransactionJournal {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $canonical = Get-CanonicalPath -Path $Path -RequireExisting
    $identity = Get-PathObjectIdentity -Path $canonical -RequireExisting
    $stream = $null
    try {
        $stream = [System.IO.File]::Open($canonical, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        if ($stream.Length -gt $script:MaxSnapshotBytes) { throw "Install transaction journal is too large: $canonical" }
        $bytes = New-Object byte[] ([int]$stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) { throw "Install transaction journal ended before its declared length: $canonical" }
            $offset += $read
        }
    }
    finally { if ($null -ne $stream) { $stream.Dispose() } }
    Assert-PathObjectIdentity -Path $canonical -ExpectedIdentity $identity | Out-Null
    $payload = [System.Text.Encoding]::UTF8.GetString($bytes)
    $value = $payload | ConvertFrom-Json
    if ($null -eq $value -or [string]$value.schema_version -ne 'hwpx/windows-install-transaction/v1') {
        throw "Install transaction journal schema is invalid: $canonical"
    }
    return [pscustomobject]@{ value = $value; object_identity = $identity; path = $canonical }
}

function Assert-NoReparsePath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'Path must not be empty.' }
    $current = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path))
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Path contains a symlink or reparse point: $current"
            }
        }
        $parent = Split-Path -Parent $current
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $current) { break }
        $current = $parent
    }
    return [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path))
}

function Assert-NoReparseSourcePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    $normalized = Assert-WindowsSafeSourceRelativePath -RelativePath $RelativePath
    $current = Assert-NoReparsePath -Path $Root
    foreach ($part in @($normalized.Split([char]92))) {
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current) {
            Assert-NoReparsePath -Path $current | Out-Null
        }
    }
    return $current
}

function Remove-PathIdentityExact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedObjectIdentity
    )
    $canonical = Assert-NoReparsePath -Path $Path
    if (-not (Test-Path -LiteralPath $canonical)) { return $false }
    if ([string]::IsNullOrWhiteSpace($ExpectedObjectIdentity)) {
        throw "Identity-bound deletion requires an expected object identity: $canonical"
    }
    [HwpxInstallNative.Identity]::DeletePathIfIdentity($canonical, $ExpectedObjectIdentity)
    if (Test-Path -LiteralPath $canonical) {
        throw "Identity-bound deletion did not remove the path: $canonical"
    }
    return $true
}

function Move-PathIdentityExact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedObjectIdentity
    )
    $sourceCanonical = Assert-NoReparsePath -Path $Source
    $destinationCanonical = Assert-NoReparsePath -Path $Destination
    if (-not (Test-Path -LiteralPath $sourceCanonical)) {
        throw "Identity-bound move source does not exist: $sourceCanonical"
    }
    if (Test-Path -LiteralPath $destinationCanonical) {
        throw "Identity-bound move destination already exists: $destinationCanonical"
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedObjectIdentity)) {
        throw "Identity-bound move requires an expected object identity: $sourceCanonical"
    }
    [HwpxInstallNative.Identity]::MovePathIfIdentity($sourceCanonical, $ExpectedObjectIdentity, $destinationCanonical)
    if (-not (Test-Path -LiteralPath $destinationCanonical)) {
        throw "Identity-bound move destination was not created: $destinationCanonical"
    }
    if (Test-Path -LiteralPath $sourceCanonical) {
        throw "Identity-bound move source remained after mutation: $sourceCanonical"
    }
    return $destinationCanonical
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "File does not exist: $Path"
    }
    $stream = $null
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::Open(
            (Assert-NoReparsePath -Path $Path),
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $sha.Dispose()
    }
}

function Copy-FileVerified {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$DestinationPath,
        [Parameter(Mandatory = $true)][int64]$ExpectedSize,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    $source = Assert-NoReparsePath -Path $SourcePath
    $sourceItem = Get-Item -LiteralPath $source -Force -ErrorAction Stop
    if ($sourceItem.PSIsContainer) { throw "Source path is not a regular file: $SourcePath" }
    if ([int64]$sourceItem.Length -ne $ExpectedSize) {
        throw "Source size changed before copy: $SourcePath"
    }
    if ($ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw "Expected SHA-256 is invalid: $SourcePath"
    }
    $destination = [System.IO.Path]::GetFullPath($DestinationPath)
    Assert-NoReparsePath -Path (Split-Path -Parent $destination) | Out-Null
    $sourceStream = $null
    $destinationStream = $null
    $sourceIdentity = Get-PathObjectIdentity -Path $source -RequireExisting
    $destinationIdentity = $null
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $buffer = New-Object byte[] (1024 * 1024)
    $copied = [int64]0
    $actualSha256 = $null
    try {
        $sourceStream = [System.IO.File]::Open($source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        if ((Get-PathObjectIdentity -Path $source -RequireExisting) -cne [string]$sourceIdentity) {
            throw "Source filesystem object changed before copy: $SourcePath"
        }
        $destinationStream = [System.IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        while (($read = $sourceStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $destinationStream.Write($buffer, 0, $read)
            [void]$sha.TransformBlock($buffer, 0, $read, $buffer, 0)
            $copied += $read
        }
        [void]$sha.TransformFinalBlock($buffer, 0, 0)
        $actualSha256 = ([System.BitConverter]::ToString($sha.Hash)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        if ($null -ne $destinationStream) { $destinationStream.Dispose() }
        if ($null -ne $sourceStream) { $sourceStream.Dispose() }
        $sha.Dispose()
    }
    if ($copied -ne $ExpectedSize -or $actualSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            $destinationIdentity = Get-PathObjectIdentity -Path $destination -RequireExisting
            Assert-PathObjectIdentity -Path $destination -ExpectedIdentity $destinationIdentity | Out-Null
        }
        Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        throw "Copied source bytes do not match the manifest: $SourcePath"
    }
    $destinationItem = Get-Item -LiteralPath $destination -Force -ErrorAction Stop
    $destinationIdentity = Get-PathObjectIdentity -Path $destination -RequireExisting
    $destinationSha256 = Get-Sha256Hex -Path $destination
    if ([int64]$destinationItem.Length -ne $ExpectedSize -or $destinationSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
        Assert-PathObjectIdentity -Path $destination -ExpectedIdentity $destinationIdentity | Out-Null
        Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        throw "Destination bytes do not match the manifest: $destination"
    }
    return [pscustomobject]@{
        source_path = $source
        destination_path = $destination
        source_size = [int64]$ExpectedSize
        source_sha256 = $ExpectedSha256.ToLowerInvariant()
        destination_size = [int64]$destinationItem.Length
        destination_sha256 = $destinationSha256
    }
}

function Test-NonEmptyFile {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        return ([int64]$item.Length -gt 0)
    }
    catch {
        return $false
    }
}

function Assert-WindowsSafeSourceRelativePath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $normalized = $RelativePath.Replace('/', '\')
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or $RelativePath.IndexOf([char]0) -ge 0 -or [IO.Path]::IsPathRooted($normalized)) {
        throw "Unsafe source member path: $RelativePath"
    }
    $reservedNames = @('CON', 'PRN', 'AUX', 'NUL') + (1..9 | ForEach-Object { "COM$_" }) + (1..9 | ForEach-Object { "LPT$_" })
    $parts = @($normalized.Split([char]92))
    foreach ($part in $parts) {
        if ([string]::IsNullOrWhiteSpace($part) -or $part -in @('.', '..') -or $part.Contains('..') -and $part -eq '..') {
            throw "Unsafe source member path: $RelativePath"
        }
        if ($part.IndexOfAny([char[]]('<>:"|?*')) -ge 0 -or $part.EndsWith(' ') -or $part.EndsWith('.')) {
            throw "Unsafe Windows source member name: $RelativePath"
        }
        foreach ($character in $part.ToCharArray()) {
            if ([int][char]$character -lt 32) { throw "Unsafe Windows source member name: $RelativePath" }
        }
        $stem = $part.Split('.', 2)[0].ToUpperInvariant()
        if ($reservedNames -contains $stem) { throw "Reserved Windows source member name: $RelativePath" }
    }
    if ($parts -contains '..') { throw "Unsafe source member path: $RelativePath" }
    return $normalized
}

function Append-NativeBackslashes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Text.StringBuilder]$Builder,
        [Parameter(Mandatory = $true)][int]$Count
    )
    if ($Count -gt 0) {
        [void]$Builder.Append([string]::new([char]92, $Count))
    }
}

function ConvertTo-NativeCommandLineArgument {
    [CmdletBinding()]
    param([AllowNull()][object]$Value)

    $text = if ($null -eq $Value) { '' } else { [string]$Value }
    # The Windows Python launcher recognizes version selectors only when they
    # remain command-line switches. Quoting -3.13 makes py.exe forward it to
    # the selected interpreter as an invalid option. Keep the exception
    # narrowly scoped; every other argument still uses the full safe quoting
    # algorithm below.
    if ($text -match '^-[0-9]+\.[0-9]+$') { return $text }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append([char]34)
    $backslashes = 0
    foreach ($character in $text.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes++
            continue
        }
        if ($character -eq [char]34) {
            Append-NativeBackslashes -Builder $builder -Count ($backslashes * 2 + 1)
            [void]$builder.Append([char]34)
            $backslashes = 0
            continue
        }
        Append-NativeBackslashes -Builder $builder -Count $backslashes
        [void]$builder.Append($character)
        $backslashes = 0
    }
    Append-NativeBackslashes -Builder $builder -Count ($backslashes * 2)
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Convert-NativeBytesToText {
    [CmdletBinding()]
    param(
        [AllowNull()][byte[]]$Bytes,
        [int]$MaxChars = $script:MaxNativeOutputChars
    )
    if ($MaxChars -lt 1) { throw 'MaxChars must be positive.' }
    if ($null -eq $Bytes -or $Bytes.Length -eq 0) {
        return [pscustomobject]@{
            text = ''
            truncated = $false
            encoding = 'none'
            decode_fallback = $false
            decode_error = ''
        }
    }

    $decoded = $null
    $encodingName = ''
    $decodeFallback = $false
    $decodeError = ''
    try {
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $decoded = $strictUtf8.GetString($Bytes)
        $encodingName = 'utf-8'
    }
    catch {
        # Native tools on Windows may write the active ANSI code page even
        # when PowerShell itself is configured for UTF-8. Decode strict UTF-8
        # first, then use the system code page without turning a valid zero
        # exit into a false capture failure. The original byte counts remain
        # authoritative and the fallback is recorded for diagnostics.
        $decodeError = Limit-Text -Value $_.Exception.Message -MaxChars 4096
        try {
            $systemEncoding = [System.Text.Encoding]::Default
            $decoded = $systemEncoding.GetString($Bytes)
            $encodingName = [string]$systemEncoding.WebName
            $decodeFallback = $true
        }
        catch {
            throw "Native output decoding failed as UTF-8 and the system code page: $($_.Exception.Message)"
        }
    }

    $truncated = $decoded.Length -gt $MaxChars
    $capturedCount = [Math]::Min($decoded.Length, $MaxChars)
    $text = if ($capturedCount -gt 0) { $decoded.Substring(0, $capturedCount) } else { '' }
    return [pscustomobject]@{
        text = [string]$text
        truncated = [bool]$truncated
        encoding = [string]$encodingName
        decode_fallback = [bool]$decodeFallback
        decode_error = [string]$decodeError
    }
}

function Stop-NativeProcessTree {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$RootPid,
        [Parameter(Mandatory = $true)][string]$RootCreationDate,
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [ValidateRange(1, 30)][int]$TimeoutSeconds = 5
    )
    if ($RootPid -le 0) { throw 'Native process tree root PID must be positive.' }
    if ([string]::IsNullOrWhiteSpace($RootCreationDate)) {
        throw "Native process tree root launch identity is required: $RootPid"
    }
    if ($JobHandle -eq [IntPtr]::Zero) {
        throw "Native process tree must be bound to an owned Windows Job Object: $RootPid"
    }

    $initialRows = @(Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object ProcessId, ParentProcessId, CreationDate)
    $initialByPid = @{}
    foreach ($row in $initialRows) { $initialByPid[[int]$row.ProcessId] = $row }
    $rootRow = if ($initialByPid.ContainsKey($RootPid)) { $initialByPid[$RootPid] } else { $null }
    if ($null -eq $rootRow) {
        if ($null -eq (Get-Process -Id $RootPid -ErrorAction SilentlyContinue)) {
            [HwpxInstallNative.Job]::Terminate($JobHandle)
            return [pscustomobject]@{
                root_pid = $RootPid
                stopped_pids = @()
                remaining_pids = @()
                released = $true
                job_terminated = $true
                late_spawn = $false
            }
        }
        throw "Native process tree launch identity could not be read: $RootPid"
    }
    if ($RootCreationDate.StartsWith('cim:', [StringComparison]::Ordinal)) {
        if ([string]::IsNullOrWhiteSpace([string]$rootRow.CreationDate) -or ('cim:' + [string]$rootRow.CreationDate) -ne $RootCreationDate) {
            throw "Native process root identity changed before tree termination: $RootPid"
        }
    }
    elseif ($RootCreationDate.StartsWith('start:', [StringComparison]::Ordinal)) {
        $rootProcess = Get-Process -Id $RootPid -ErrorAction SilentlyContinue
        if ($null -eq $rootProcess) { throw "Native process root disappeared before owned-job termination: $RootPid" }
        try { $observedStartIdentity = 'start:' + $rootProcess.StartTime.ToUniversalTime().Ticks }
        catch { throw "Native process root launch identity could not be read: $RootPid" }
        if ($observedStartIdentity -ne $RootCreationDate) { throw "Native process root identity changed before tree termination: $RootPid" }
    }
    else {
        throw "Native process root launch identity has an unsupported format: $RootPid"
    }

    # Terminating the owned job is the authoritative operation.  The dynamic
    # membership scan below is only a release proof; it is deliberately not a
    # PID-based substitute for job ownership and catches children that appeared
    # after any earlier diagnostic snapshot.
    [HwpxInstallNative.Job]::Terminate($JobHandle)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $remaining = @()
    $lateSpawnObserved = $false
    do {
        $rows = @(Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object ProcessId, ParentProcessId, CreationDate)
        $byPid = @{}
        foreach ($row in $rows) { $byPid[[int]$row.ProcessId] = $row }
        $remainingList = New-Object System.Collections.ArrayList
        foreach ($row in $rows) {
            $candidatePid = [int]$row.ProcessId
            $cursor = $candidatePid
            $belongsToRoot = $false
            for ($depth = 0; $depth -le $rows.Count; $depth++) {
                if ($cursor -eq $RootPid) { $belongsToRoot = $true; break }
                if (-not $byPid.ContainsKey($cursor)) { break }
                $cursor = [int]$byPid[$cursor].ParentProcessId
            }
            if ($belongsToRoot) {
                if (-not $initialByPid.ContainsKey($candidatePid)) { $lateSpawnObserved = $true }
                [void]$remainingList.Add($candidatePid)
            }
        }
        $remaining = @($remainingList | Sort-Object -Unique)
        if ($remaining.Count -eq 0) { break }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    return [pscustomobject]@{
        root_pid = $RootPid
        stopped_pids = @($RootPid)
        remaining_pids = @($remaining)
        released = ($remaining.Count -eq 0)
        job_terminated = $true
        late_spawn = [bool]$lateSpawnObserved
    }
}

# `$process.Kill()` alone is intentionally not used for timed-out native work;
# termination must cover the verified descendant tree as well.

function Invoke-NativeChecked {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = (Get-Location).Path,
        [int[]]$AcceptExitCodes = @(0),
        [switch]$AllowNonZero,
        [string]$ReceiptPath,
        [ValidateRange(1, 600)][int]$TimeoutSeconds = $script:DefaultNativeTimeoutSeconds,
        [ValidateRange(1, 67108864)][int]$MaxOutputBytes = $script:MaxNativeOutputBytes
    )

    $working = Get-CanonicalPath -Path $WorkingDirectory -RequireExisting
    $exitCode = -1
    $invocationError = ''
    $captureError = ''
    $timedOut = $false
    $processKilled = $false
    $terminationError = ''
    $processId = 0
    $processStarted = $false
    $nativeProcessResumed = $false
    $processExitConfirmed = $false
    $nativeRootCreationDate = $null
    $nativeJobHandle = [IntPtr]::Zero
    $nativeJobAssigned = $false
    $nativeJobTerminated = $false
    $identityCaptureFailed = $false
    $nativeTreeStop = $null
    $nativeTreeReleased = $false
    $nativeFaultMode = [string]([Environment]::GetEnvironmentVariable('HWPX_TEST_NATIVE_FAULT'))
    if ($nativeFaultMode -notin @('', 'invocation', 'capture', 'decode', 'drain', 'termination', 'identity')) {
        $nativeFaultMode = ''
    }
    $process = $null
    $stdoutStream = $null
    $stderrStream = $null
    $stdoutBuffer = New-Object byte[] 8192
    $stderrBuffer = New-Object byte[] 8192
    $stdoutTask = $null
    $stderrTask = $null
    $stdoutDone = $false
    $stderrDone = $false
    $stdoutCapture = New-Object System.IO.MemoryStream
    $stderrCapture = New-Object System.IO.MemoryStream
    $stdoutObservedBytes = 0L
    $stderrObservedBytes = 0L
    $stdoutCapturedBytes = 0L
    $stderrCapturedBytes = 0L
    $stdoutTruncated = $false
    $stderrTruncated = $false
    $stdoutTextTruncated = $false
    $stderrTextTruncated = $false
    $stdoutEncoding = ''
    $stderrEncoding = ''
    $stdoutDecodeFallback = $false
    $stderrDecodeFallback = $false
    $stdoutDecodeError = ''
    $stderrDecodeError = ''
    $stdout = ''
    $stderr = ''
    try {
        $commandAvailable = ($null -ne (Get-Command -Name $FilePath -ErrorAction SilentlyContinue)) -or
            (Test-Path -LiteralPath $FilePath -PathType Leaf)
        if (-not $commandAvailable) {
            throw "Native command was not found: $FilePath"
        }
        if ($nativeFaultMode -eq 'invocation') {
            throw 'injected native invocation failure'
        }

        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $FilePath
        $argumentValues = @($Arguments | ForEach-Object { ConvertTo-NativeCommandLineArgument -Value $_ })
        $startInfo.Arguments = [string]::Join(' ', [string[]]$argumentValues)
        $startInfo.WorkingDirectory = $working
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true

        $nativeJobHandle = [HwpxInstallNative.Job]::CreateKillOnCloseJob()
        $nativeCommandLine = '"' + $FilePath + '"'
        if ($argumentValues.Count -gt 0) {
            $nativeCommandLine += ' ' + [string]::Join(' ', [string[]]$argumentValues)
        }
        # CreateProcess is suspended, so no child code can spawn before the
        # process is attached to this run's kill-on-close job object.
        $process = [HwpxInstallNative.SuspendedProcess]::Create($FilePath, $nativeCommandLine, $working)
        $processStarted = $true
        $processId = [int]$process.Id
        [HwpxInstallNative.Job]::AssignProcess($nativeJobHandle, $processId)
        $nativeJobAssigned = $true
        $process.Resume()
        $nativeProcessResumed = $true
        if ($nativeFaultMode -eq 'identity') {
            $identityCaptureFailed = $true
            throw 'injected native launch identity capture failure'
        }
        try {
            $identityDeadline = (Get-Date).AddSeconds(5)
            do {
                try {
                    $nativeRoot = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop
                    if ($nativeRoot) { $nativeRootCreationDate = [string]$nativeRoot.CreationDate }
                }
                catch {
                    $nativeRootCreationDate = $null
                }
                if (-not [string]::IsNullOrWhiteSpace($nativeRootCreationDate)) {
                    $nativeRootCreationDate = 'cim:' + $nativeRootCreationDate
                }
                else {
                    try {
                        # Some Windows hosts expose Win32_Process.CreationDate
                        # as null even though the managed process handle has a
                        # stable StartTime.  Keep a typed launch identity
                        # rather than silently degrading to PID-only checks.
                        $nativeStartTicks = $process.ManagedProcess.StartTime.ToUniversalTime().Ticks
                        if ($nativeStartTicks -gt 0) { $nativeRootCreationDate = 'start:' + $nativeStartTicks }
                    }
                    catch { $nativeRootCreationDate = $null }
                }
                if (-not [string]::IsNullOrWhiteSpace($nativeRootCreationDate)) { break }
                Start-Sleep -Milliseconds 50
            } while ((Get-Date) -lt $identityDeadline)
        }
        catch {
            $nativeRootCreationDate = $null
        }
        if ([string]::IsNullOrWhiteSpace($nativeRootCreationDate)) {
            $identityCaptureFailed = $true
            throw "Native process launch identity could not be captured: $processId"
        }
        $stdoutStream = $process.OpenStdoutStream()
        $stderrStream = $process.OpenStderrStream()
        if ($nativeFaultMode -eq 'capture') {
            $captureError = 'injected native capture failure'
        }
        # Read both byte streams concurrently. Sequential reads can deadlock
        # when a native child fills stderr while stdout is being consumed.
        $stdoutTask = $stdoutStream.ReadAsync($stdoutBuffer, 0, $stdoutBuffer.Length)
        $stderrTask = $stderrStream.ReadAsync($stderrBuffer, 0, $stderrBuffer.Length)
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        $drainDeadline = $null
        while (-not ($stdoutDone -and $stderrDone)) {
            $madeProgress = $false
            if (-not $stdoutDone -and $stdoutTask.IsCompleted) {
                try {
                    $readCount = [int]$stdoutTask.GetAwaiter().GetResult()
                }
                catch {
                    $captureError = "stdout capture failed: $($_.Exception.Message)"
                    $readCount = 0
                    $stdoutDone = $true
                }
                if ($readCount -le 0) {
                    $stdoutDone = $true
                }
                else {
                    $stdoutObservedBytes += [int64]$readCount
                    $remaining = [int64]$MaxOutputBytes - $stdoutCapturedBytes
                    $copyCount = [Math]::Min([int64]$readCount, [Math]::Max(0L, $remaining))
                    if ($copyCount -gt 0) {
                        $stdoutCapture.Write($stdoutBuffer, 0, [int]$copyCount)
                        $stdoutCapturedBytes += $copyCount
                    }
                    if ($copyCount -lt $readCount) { $stdoutTruncated = $true }
                    $stdoutTask = $stdoutStream.ReadAsync($stdoutBuffer, 0, $stdoutBuffer.Length)
                }
                $madeProgress = $true
            }
            if (-not $stderrDone -and $stderrTask.IsCompleted) {
                try {
                    $readCount = [int]$stderrTask.GetAwaiter().GetResult()
                }
                catch {
                    if ($captureError) { $captureError += '; ' }
                    $captureError += "stderr capture failed: $($_.Exception.Message)"
                    $readCount = 0
                    $stderrDone = $true
                }
                if ($readCount -le 0) {
                    $stderrDone = $true
                }
                else {
                    $stderrObservedBytes += [int64]$readCount
                    $remaining = [int64]$MaxOutputBytes - $stderrCapturedBytes
                    $copyCount = [Math]::Min([int64]$readCount, [Math]::Max(0L, $remaining))
                    if ($copyCount -gt 0) {
                        $stderrCapture.Write($stderrBuffer, 0, [int]$copyCount)
                        $stderrCapturedBytes += $copyCount
                    }
                    if ($copyCount -lt $readCount) { $stderrTruncated = $true }
                    $stderrTask = $stderrStream.ReadAsync($stderrBuffer, 0, $stderrBuffer.Length)
                }
                $madeProgress = $true
            }

            if (-not $timedOut -and [DateTime]::UtcNow -ge $deadline) {
                $timedOut = $true
                $drainDeadline = [DateTime]::UtcNow.AddSeconds(5)
                try {
                    $nativeTreeStop = Stop-NativeProcessTree -RootPid $processId -RootCreationDate $nativeRootCreationDate -JobHandle $nativeJobHandle -TimeoutSeconds 5
                    $processKilled = @($nativeTreeStop.stopped_pids).Count -gt 0
                    $nativeTreeReleased = [bool]$nativeTreeStop.released
                    $nativeJobTerminated = [bool]$nativeTreeStop.job_terminated
                }
                catch {
                    $terminationError = $_.Exception.Message
                }
                $madeProgress = $true
            }
            if ($timedOut -and $null -ne $drainDeadline -and [DateTime]::UtcNow -ge $drainDeadline -and -not ($stdoutDone -and $stderrDone)) {
                if (-not $stdoutDone) { try { $stdoutStream.Close() } catch { }; $stdoutDone = $true }
                if (-not $stderrDone) { try { $stderrStream.Close() } catch { }; $stderrDone = $true }
                $captureError = if ($captureError) { $captureError + '; native stream drain timed out after process termination' } else { 'native stream drain timed out after process termination' }
                $madeProgress = $true
            }
            if (-not $madeProgress) {
                Start-Sleep -Milliseconds 10
            }
        }

        if ($nativeFaultMode -eq 'drain') {
            $captureError = if ($captureError) { $captureError + '; ' } else { '' }
            $captureError += 'injected native stream drain failure'
        }
        if (-not $timedOut -and -not $process.HasExited) {
            $remainingMilliseconds = [int][Math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalMilliseconds)
            if ($remainingMilliseconds -le 0 -or -not $process.WaitForExit($remainingMilliseconds)) {
                $timedOut = $true
                try {
                    $nativeTreeStop = Stop-NativeProcessTree -RootPid $processId -RootCreationDate $nativeRootCreationDate -JobHandle $nativeJobHandle -TimeoutSeconds 5
                    $processKilled = @($nativeTreeStop.stopped_pids).Count -gt 0
                    $nativeTreeReleased = [bool]$nativeTreeStop.released
                    $nativeJobTerminated = [bool]$nativeTreeStop.job_terminated
                }
                catch {
                    $terminationError = $_.Exception.Message
                }
            }
        }
        if ($timedOut) {
            try {
                if (-not $process.HasExited) {
                    $process.WaitForExit(5000) | Out-Null
                }
            }
            catch { }
            try { $processExitConfirmed = [bool]$process.HasExited } catch { $processExitConfirmed = $false }
            if (-not $process.HasExited -or -not $nativeTreeReleased) {
                $terminationError = if ($terminationError) { $terminationError + '; ' } else { '' }
                $terminationError += 'native process tree remained active after termination wait'
            }
            $exitCode = -2
        }
        else {
            if (-not $process.HasExited) {
                throw 'Native process exit could not be confirmed.'
            }
            $processExitConfirmed = $true
            $nativeTreeReleased = $true
            $exitCode = [int]$process.ExitCode
        }
        if ($nativeFaultMode -eq 'termination') {
            $terminationError = 'injected native termination/readback failure'
        }
    }
    catch {
        $invocationError = $_.Exception.Message
        $exitCode = -1
        if ($nativeJobAssigned -and $nativeJobHandle -ne [IntPtr]::Zero -and -not [string]::IsNullOrWhiteSpace($nativeRootCreationDate)) {
            try {
                $nativeTreeStop = Stop-NativeProcessTree -RootPid $processId -RootCreationDate $nativeRootCreationDate -JobHandle $nativeJobHandle -TimeoutSeconds 5
                $processKilled = @($nativeTreeStop.stopped_pids).Count -gt 0
                $nativeTreeReleased = [bool]$nativeTreeStop.released
                $nativeJobTerminated = [bool]$nativeTreeStop.job_terminated
            }
            catch {
                $terminationError = $_.Exception.Message
            }
        }
        elseif ($nativeJobAssigned -and $nativeJobHandle -ne [IntPtr]::Zero) {
            # Job ownership is still authoritative when CIM identity capture
            # itself failed.  Do not fall back to PID-only tree termination;
            # terminate the owned group and report the missing identity as a
            # fail-closed invocation error.
            try {
                [HwpxInstallNative.Job]::Terminate($nativeJobHandle)
                $processKilled = $true
                $nativeJobTerminated = $true
                $terminationError = 'native process launch identity was unavailable; owned job was terminated without PID-only fallback'
                if ($null -ne $process -and $process.WaitForExit(5000)) {
                    $processExitConfirmed = $true
                }
            }
            catch {
                $terminationError = $_.Exception.Message
            }
        }
        elseif ($processStarted -and $null -ne $process) {
            # Assignment failed before the process entered the job.  Kill via
            # the already-owned Process object only as a containment action,
            # never by a reusable PID, and keep the result rejected.
            try {
                if (-not $process.HasExited) { $process.Kill() }
                $process.WaitForExit(5000) | Out-Null
                $processKilled = $true
                $processExitConfirmed = [bool]$process.HasExited
                $terminationError = 'native process could not be assigned to its owned job object'
            }
            catch {
                $terminationError = $_.Exception.Message
            }
        }
    }
    try {
        $stdoutRaw = $stdoutCapture.ToArray()
        $stderrRaw = $stderrCapture.ToArray()
        if ($nativeFaultMode -eq 'decode') {
            throw 'injected native output decode failure'
        }
        $stdoutDecoded = Convert-NativeBytesToText -Bytes $stdoutRaw -MaxChars $script:MaxNativeOutputChars
        $stderrDecoded = Convert-NativeBytesToText -Bytes $stderrRaw -MaxChars $script:MaxNativeOutputChars
        $stdoutTextTruncated = [bool]$stdoutDecoded.truncated
        $stderrTextTruncated = [bool]$stderrDecoded.truncated
        $stdoutEncoding = [string]$stdoutDecoded.encoding
        $stderrEncoding = [string]$stderrDecoded.encoding
        $stdoutDecodeFallback = [bool]$stdoutDecoded.decode_fallback
        $stderrDecodeFallback = [bool]$stderrDecoded.decode_fallback
        $stdoutDecodeError = [string]$stdoutDecoded.decode_error
        $stderrDecodeError = [string]$stderrDecoded.decode_error
        $stdout = [string]$stdoutDecoded.text
        $stderr = [string]$stderrDecoded.text
    }
    catch {
        $stdout = ''
        $stderr = ''
        if ($captureError) { $captureError += '; ' }
        $captureError += "native output decoding failed: $($_.Exception.Message)"
    }
    if ($captureError) {
        $invocationError = if ($invocationError) { $invocationError + '; ' + $captureError } else { $captureError }
    }
    if ($terminationError) {
        $invocationError = if ($invocationError) { $invocationError + '; ' + $terminationError } else { $terminationError }
    }
    if ($timedOut) {
        $timeoutMessage = "Native command exceeded timeout of $TimeoutSeconds seconds"
        $invocationError = if ($invocationError) { $invocationError + '; ' + $timeoutMessage } else { $timeoutMessage }
    }
    if ($invocationError) {
        $stderr = Limit-Text -Value ($stderr + "`n" + $invocationError) -MaxChars $script:MaxNativeOutputChars
    }
    try {
        $result = [ordered]@{
            command = [string]$FilePath
            arguments = @($Arguments | ForEach-Object { [string]$_ })
            working_directory = [string]$working
            exit_code = [int]$exitCode
            stdout = [string]$stdout
            stderr = [string]$stderr
            stdout_bytes = [int64]$stdoutObservedBytes
            stderr_bytes = [int64]$stderrObservedBytes
            stdout_captured_bytes = [int64]$stdoutCapturedBytes
            stderr_captured_bytes = [int64]$stderrCapturedBytes
            stdout_truncated = [bool]($stdoutTruncated -or $stdoutTextTruncated)
            stderr_truncated = [bool]($stderrTruncated -or $stderrTextTruncated)
            stdout_encoding = [string]$stdoutEncoding
            stderr_encoding = [string]$stderrEncoding
            stdout_decode_fallback = [bool]$stdoutDecodeFallback
            stderr_decode_fallback = [bool]$stderrDecodeFallback
            stdout_decode_error = Limit-Text -Value $stdoutDecodeError -MaxChars 4096
            stderr_decode_error = Limit-Text -Value $stderrDecodeError -MaxChars 4096
            capture_error = [string]$captureError
            termination_error = [string]$terminationError
            invocation_error = [string]$invocationError
            accepted = (($AcceptExitCodes -contains $exitCode) -and [string]::IsNullOrWhiteSpace($invocationError) -and [string]::IsNullOrWhiteSpace($captureError) -and [string]::IsNullOrWhiteSpace($terminationError) -and -not $timedOut -and -not $processKilled -and $processExitConfirmed -and $nativeJobAssigned -and -not $identityCaptureFailed)
            exit_confirmed = [bool]$processExitConfirmed
            capture_mode = 'raw-byte-pipes'
            max_output_bytes = [int]$MaxOutputBytes
            timeout_seconds = [int]$TimeoutSeconds
            process_id = [int]$processId
            timed_out = [bool]$timedOut
            process_killed = [bool]$processKilled
            process_tree_released = [bool]$nativeTreeReleased
            process_tree_stopped_pids = if ($nativeTreeStop) { @($nativeTreeStop.stopped_pids) } else { @() }
            process_tree_remaining_pids = if ($nativeTreeStop) { @($nativeTreeStop.remaining_pids) } else { @() }
            job_object_owned = [bool]$nativeJobAssigned
            job_object_terminated = [bool]$nativeJobTerminated
            process_created_suspended = $true
            process_resumed_after_job_assignment = [bool]$nativeProcessResumed
            launch_identity = [ordered]@{
                process_id = [int]$processId
                creation_date = [string]$nativeRootCreationDate
                ownership = 'windows-job-object'
                assigned = [bool]$nativeJobAssigned
            }
            identity_capture_failed = [bool]$identityCaptureFailed
            late_spawn = [bool]($nativeTreeStop -and $nativeTreeStop.late_spawn)
            fault_injection = [string]$nativeFaultMode
        }
        if ($ReceiptPath) {
            Write-JsonReceipt -Path $ReceiptPath -Value $result
        }
        if (-not $AllowNonZero -and -not ($AcceptExitCodes -contains $exitCode)) {
            throw "Native command failed with exit code ${exitCode}: $FilePath $($Arguments -join ' ')"
        }
        return [pscustomobject]$result
    }
    finally {
        if ($null -ne $stdoutStream) { $stdoutStream.Dispose() }
        if ($null -ne $stderrStream) { $stderrStream.Dispose() }
        if ($null -ne $process) { $process.Dispose() }
        if ($null -ne $stdoutCapture) { $stdoutCapture.Dispose() }
        if ($null -ne $stderrCapture) { $stderrCapture.Dispose() }
        if ($nativeJobHandle -ne [IntPtr]::Zero) {
            try { [HwpxInstallNative.Job]::Close($nativeJobHandle) } catch { }
        }
    }
}

function ConvertTo-RepositoryIdentity {
    param([Parameter(Mandatory = $true)][string]$Value)
    $normalized = $Value.Trim().Replace([char]92, '/')
    $normalized = $normalized -replace '\.git$', ''
    if ($normalized -match '(?i)^git@github\.com:(.+)$') { return ('github:' + $Matches[1]).ToLowerInvariant() }
    if ($normalized -match '(?i)^https?://github\.com/(.+)$') { return ('github:' + $Matches[1]).ToLowerInvariant() }
    if ($normalized -match '(?i)^ssh://git@github\.com/(.+)$') { return ('github:' + $Matches[1]).ToLowerInvariant() }
    return $normalized.ToLowerInvariant()
}

function Get-IndependentGitIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$SourceRoot)

    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($null -eq $git) { return $null }
    # Windows PowerShell 5.1 promotes native stderr to an ErrorRecord when
    # $ErrorActionPreference is Stop.  A git-less source bundle is expected to
    # make the first probe return nonzero, so route every probe through the
    # bounded native helper and inspect its direct exit code instead.
    $rootProbe = Invoke-NativeChecked -FilePath $git.Source -Arguments @('-C', $SourceRoot, 'rev-parse', '--show-toplevel') -WorkingDirectory $SourceRoot -AllowNonZero
    $rootValue = ([string]$rootProbe.stdout).Trim()
    if ($rootProbe.exit_code -ne 0 -or [string]::IsNullOrWhiteSpace($rootValue)) {
        return $null
    }
    $gitRoot = Get-CanonicalPath -Path $rootValue -RequireExisting
    $expectedRoot = Get-CanonicalPath -Path $SourceRoot -RequireExisting
    if ($gitRoot -cne $expectedRoot) { return $null }
    $statusProbe = Invoke-NativeChecked -FilePath $git.Source -Arguments @(
        '-C', $expectedRoot, 'status', '--porcelain=v1', '-z', '--untracked-files=all'
    ) -WorkingDirectory $expectedRoot -AllowNonZero
    if ($statusProbe.exit_code -ne 0) {
        throw "Independent Git checkout status could not be read: $expectedRoot"
    }
    $dirtyRecords = @(
        ([string]$statusProbe.stdout -split [char]0) |
            Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
    )
    if ($dirtyRecords.Count -gt 0) {
        throw "Independent Git checkout is not clean: $expectedRoot"
    }
    $remoteProbe = Invoke-NativeChecked -FilePath $git.Source -Arguments @('-C', $expectedRoot, 'config', '--get', 'remote.origin.url') -WorkingDirectory $expectedRoot -AllowNonZero
    $commitProbe = Invoke-NativeChecked -FilePath $git.Source -Arguments @('-C', $expectedRoot, 'rev-parse', '--verify', 'HEAD') -WorkingDirectory $expectedRoot -AllowNonZero
    $treeProbe = Invoke-NativeChecked -FilePath $git.Source -Arguments @('-C', $expectedRoot, 'rev-parse', '--verify', 'HEAD^{tree}') -WorkingDirectory $expectedRoot -AllowNonZero
    $remoteValue = ([string]$remoteProbe.stdout).Trim()
    $commitValue = ([string]$commitProbe.stdout).Trim()
    $treeValue = ([string]$treeProbe.stdout).Trim()
    if ($remoteProbe.exit_code -ne 0 -or [string]::IsNullOrWhiteSpace($remoteValue) -or
        $commitProbe.exit_code -ne 0 -or $treeProbe.exit_code -ne 0 -or
        [string]::IsNullOrWhiteSpace($commitValue) -or [string]::IsNullOrWhiteSpace($treeValue)) {
        throw "Independent Git source identity could not be read: $expectedRoot"
    }
    $commit = $commitValue
    $tree = $treeValue
    if ($commit -notmatch '^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$' -or $tree -notmatch '^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$') {
        throw "Independent Git source identity has invalid commit/tree syntax: $expectedRoot"
    }
    return [pscustomobject]@{
        source = 'git'
        repository_root = $gitRoot
        repository = ConvertTo-RepositoryIdentity -Value $remoteValue
        commit = $commit.ToLowerInvariant()
        tree = $tree.ToLowerInvariant()
    }
}

function Get-GitSourceMemberIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Commit
    )
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($null -eq $git) { throw 'Git is required to bind a checkout source member to its tree blob.' }
    $relativePosix = $RelativePath.Replace([char]92, [char]47)
    $treeProbe = Invoke-NativeChecked -FilePath $git.Source -Arguments @('-C', $SourceRoot, 'ls-tree', '-z', $Commit, '--', $relativePosix) -WorkingDirectory $SourceRoot -AllowNonZero
    $treeRecord = ([string]$treeProbe.stdout).Trim([char[]]@([char]0, [char]13, [char]10))
    $treeMatch = [regex]::Match($treeRecord, '^(?<mode>100644|100755)\s+(?<type>blob)\s+(?<object>[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?)\t(?<path>.*)$')
    if ($treeProbe.exit_code -ne 0 -or -not $treeMatch.Success -or [string]$treeMatch.Groups['path'].Value -cne $relativePosix) {
        throw "Git tree blob/mode could not be read for admitted source member: $RelativePath"
    }
    $expectedBlob = [string]$treeMatch.Groups['object'].Value
    $expectedMode = [string]$treeMatch.Groups['mode'].Value
    $candidate = Assert-NoReparseSourcePath -Root (Get-CanonicalPath -Path $SourceRoot -RequireExisting) -RelativePath $RelativePath
    $actualProbe = Invoke-NativeChecked -FilePath $git.Source -Arguments @('-C', $SourceRoot, 'hash-object', '--no-filters', '--', $candidate) -WorkingDirectory $SourceRoot -AllowNonZero
    $actualBlob = ([string]$actualProbe.stdout).Trim().ToLowerInvariant()
    if ($actualProbe.exit_code -ne 0 -or $actualBlob -notmatch '^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$') {
        throw "Git could not hash admitted source member bytes: $RelativePath"
    }
    $indexProbe = Invoke-NativeChecked -FilePath $git.Source -Arguments @('-C', $SourceRoot, 'ls-files', '--stage', '--', $relativePosix) -WorkingDirectory $SourceRoot -AllowNonZero
    $indexRecord = ([string]$indexProbe.stdout).Trim([char[]]@([char]0, [char]13, [char]10))
    $indexMatch = [regex]::Match($indexRecord, '^(?<mode>100644|100755)\s+[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\s+\d+\t(?<path>.*)$')
    $actualMode = if ($indexMatch.Success) { [string]$indexMatch.Groups['mode'].Value } else { '' }
    return [pscustomobject]@{
        relative_path = $relativePosix
        expected_blob = $expectedBlob.ToLowerInvariant()
        actual_blob = $actualBlob.ToLowerInvariant()
        expected_mode = $expectedMode
        actual_mode = $actualMode
        matched = ($expectedBlob -ceq $actualBlob)
        mode_matched = ($indexProbe.exit_code -eq 0 -and $indexMatch.Success -and $actualMode -ceq $expectedMode -and [string]$indexMatch.Groups['path'].Value -ceq $relativePosix)
    }
}

function Get-SourceManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [string]$ExpectedRepository,
        [string]$ExpectedCommit,
        [string]$ExpectedTree,
        [string]$ExpectedManifestSha256,
        [AllowNull()][object]$ExpectedRuntimeEnvContract
    )

    Assert-NoReparsePath -Path $SourceRoot | Out-Null
    $root = Get-CanonicalPath -Path $SourceRoot -RequireExisting
    $manifestFile = Get-CanonicalPath -Path $ManifestPath -RequireExisting
    $manifestIdentityBefore = Get-PathObjectIdentity -Path $manifestFile -RequireExisting
    $manifestStream = $null
    $manifest_bytes = $null
    $manifestHash = $null
    try {
        # Keep one read handle open while reading and hashing. ReadAllText
        # followed by hashing the pathname allowed a replacement manifest to
        # be parsed from one object and authenticated from another.
        $manifestStream = [IO.File]::Open(
            $manifestFile,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        $manifestLength = [int64]$manifestStream.Length
        if ($manifestLength -gt $script:MaxManifestBytes) {
            throw "Source manifest exceeds the bounded size limit of $script:MaxManifestBytes bytes: $manifestFile"
        }
        $manifest_bytes = New-Object byte[] ([int]$manifestLength)
        $manifestOffset = 0
        while ($manifestOffset -lt $manifest_bytes.Length) {
            $readCount = $manifestStream.Read($manifest_bytes, $manifestOffset, $manifest_bytes.Length - $manifestOffset)
            if ($readCount -le 0) { throw "Source manifest read ended before the declared file length: $manifestFile" }
            $manifestOffset += $readCount
        }
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $manifestHash = ([System.BitConverter]::ToString($sha.ComputeHash($manifest_bytes))).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $sha.Dispose()
        }
        $manifestText = [System.Text.Encoding]::UTF8.GetString($manifest_bytes)
        $manifest = $manifestText | ConvertFrom-Json
    }
    catch {
        throw "Source manifest is not valid JSON: $manifestFile"
    }
    finally {
        if ($null -ne $manifestStream) { $manifestStream.Dispose() }
    }
    Assert-PathObjectIdentity -Path $manifestFile -ExpectedIdentity $manifestIdentityBefore | Out-Null
    if ($null -eq $manifest_bytes -or [int64]$manifest_bytes.Length -gt $script:MaxManifestBytes) {
        throw "Source manifest bytes were not safely captured: $manifestFile"
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedManifestSha256)) {
        if ($ExpectedManifestSha256 -notmatch '^[0-9a-fA-F]{64}$' -or $manifestHash -cne $ExpectedManifestSha256.ToLowerInvariant()) {
            throw 'Source manifest bytes do not match the independent expected manifest SHA-256.'
        }
    }
    $mismatches = @()
    $seen = @{}
    if ($manifest.PSObject.Properties.Name -notcontains 'identity_source' -or $manifest.PSObject.Properties.Name -notcontains 'identity_verified') {
        throw 'Source manifest must explicitly declare identity provenance.'
    }
    $identitySource = [string]$manifest.identity_source
    $identityVerified = [bool]$manifest.identity_verified
    if ([string]$manifest.schema_version -ne 'hwpx/source-bundle/v1' -or [string]::IsNullOrWhiteSpace([string]$manifest.repository) -or [string]::IsNullOrWhiteSpace([string]$manifest.commit) -or [string]::IsNullOrWhiteSpace([string]$manifest.tree)) {
        throw 'Source manifest is missing the required source-bundle identity fields.'
    }
    if ($identitySource -notin @('git', 'asserted-gitless') -or
        ($identitySource -eq 'git' -and -not $identityVerified) -or
        ($identitySource -eq 'asserted-gitless' -and $identityVerified)) {
        throw 'Source manifest has an invalid or unverified Git identity declaration.'
    }
    $independentGit = $null
    $externalIdentityValues = @($ExpectedRepository, $ExpectedCommit, $ExpectedTree)
    $externalIdentityCount = @($externalIdentityValues | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }).Count
    if ($externalIdentityCount -notin @(0, 3)) {
        throw 'Expected source identity must include repository, commit, and tree together.'
    }
    if ($externalIdentityCount -eq 3) {
        if ([string]$ExpectedRepository -cne ([string]$ExpectedRepository).Trim() -or
            [string]$ExpectedRepository -match '[\x00-\x1F\x7F]' -or
            [string]$ExpectedCommit -notmatch '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$' -or
            [string]$ExpectedTree -notmatch '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$') {
            throw 'Expected source identity contains a malformed repository, commit, or tree value.'
        }
    }
    if ($identitySource -eq 'git') {
        $independentGit = Get-IndependentGitIdentity -SourceRoot $root
        if ($null -eq $independentGit -and $externalIdentityCount -ne 3) {
            throw 'Git source identity requires an independent Git checkout or a complete external identity binding.'
        }
        if ($null -eq $independentGit -and [string]::IsNullOrWhiteSpace($ExpectedManifestSha256)) {
            throw 'Git source identity without an independent checkout requires an externally supplied manifest SHA-256.'
        }
        if ($null -ne $independentGit) {
            if ((ConvertTo-RepositoryIdentity -Value ([string]$manifest.repository)) -cne [string]$independentGit.repository -or [string]$manifest.commit -cne [string]$independentGit.commit -or [string]$manifest.tree -cne [string]$independentGit.tree) {
                throw 'Source manifest Git repository/commit/tree does not match independently read Git identity.'
            }
        }
    }
    if ($identitySource -eq 'asserted-gitless' -and $externalIdentityCount -ne 3) {
        throw 'Git-less source identity requires a complete external repository/commit/tree binding.'
    }
    if ($externalIdentityCount -eq 3) {
        if ([string]$manifest.repository -cne [string]$ExpectedRepository -or
            [string]$manifest.commit -cne [string]$ExpectedCommit -or
            [string]$manifest.tree -cne [string]$ExpectedTree) {
            throw 'Source manifest identity does not match the independent external binding.'
        }
    }
    $identityBindingVerified = (
        ($identitySource -eq 'git' -and ($null -ne $independentGit -or $externalIdentityCount -eq 3)) -or
        ($identitySource -eq 'asserted-gitless' -and $externalIdentityCount -eq 3)
    )
    $runtimeEnvContractApplied = $false
    $runtimeEnvContract = $null
    $runtimeEnvExpectedPath = $null
    $runtimeEnvExpectedSize = -1
    $runtimeEnvExpectedSha256 = $null
    $runtimeEnvObserved = $false
    if ($null -ne $ExpectedRuntimeEnvContract) {
        $runtimeEnvContract = $ExpectedRuntimeEnvContract
        $contractProperties = @($runtimeEnvContract.PSObject.Properties.Name)
        foreach ($requiredContractProperty in @(
            'schema_version', 'provenance', 'source', 'path', 'install_root',
            'install_root_identity', 'size', 'sha256', 'source_manifest_sha256',
            'candidate_generation', 'created_at_utc'
        )) {
            if ($contractProperties -notcontains $requiredContractProperty) {
                throw "Runtime .env provenance contract is missing '$requiredContractProperty'."
            }
        }
        if ([string]$runtimeEnvContract.schema_version -cne 'hwpx/installer-runtime-env/v1') {
            throw 'Runtime .env provenance contract schema is unsupported.'
        }
        $runtimeEnvExpectedPath = Assert-WindowsSafeSourceRelativePath -RelativePath ([string]$runtimeEnvContract.path)
        if ($runtimeEnvExpectedPath -ine '.env') {
            throw 'Runtime .env provenance contract must bind the exact root-relative .env path.'
        }
        if ([string]::IsNullOrWhiteSpace([string]$runtimeEnvContract.install_root)) {
            throw 'Runtime .env provenance contract install root is missing.'
        }
        $contractRoot = Get-CanonicalPath -Path ([string]$runtimeEnvContract.install_root) -RequireExisting
        if ($contractRoot -cne $root) {
            throw 'Runtime .env provenance contract is bound to a different install root.'
        }
        $actualRootIdentity = Get-PathObjectIdentity -Path $root -RequireExisting
        if ([string]$runtimeEnvContract.install_root_identity -cne [string]$actualRootIdentity) {
            throw 'Runtime .env provenance contract install-root identity changed.'
        }
        $parsedRuntimeEnvSize = -1
        if (-not [int64]::TryParse([string]$runtimeEnvContract.size, [ref]$parsedRuntimeEnvSize) -or
            $parsedRuntimeEnvSize -lt 0 -or $parsedRuntimeEnvSize -gt $script:MaxManifestBytes) {
            throw 'Runtime .env provenance contract size is invalid or exceeds the bounded limit.'
        }
        $runtimeEnvExpectedSize = $parsedRuntimeEnvSize
        $runtimeEnvExpectedSha256 = [string]$runtimeEnvContract.sha256
        if ($runtimeEnvExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
            throw 'Runtime .env provenance contract SHA-256 is invalid.'
        }
        if ([string]$runtimeEnvContract.source_manifest_sha256 -cne [string]$manifestHash) {
            throw 'Runtime .env provenance contract does not match the verified source manifest bytes.'
        }
        $expectedCandidateGeneration = '{0}:{1}:{2}' -f $manifest.commit, $manifest.tree, $manifestHash
        if ([string]$runtimeEnvContract.candidate_generation -cne $expectedCandidateGeneration) {
            throw 'Runtime .env provenance contract does not match the verified candidate generation.'
        }
        if ([string]::IsNullOrWhiteSpace([string]$runtimeEnvContract.created_at_utc)) {
            throw 'Runtime .env provenance contract creation time is missing.'
        }
        $runtimeEnvProvenance = [string]$runtimeEnvContract.provenance
        $runtimeEnvSource = [string]$runtimeEnvContract.source
        if ($runtimeEnvProvenance -notin @('installer-generated', 'installer-preserved')) {
            throw 'Runtime .env provenance contract provenance is unsupported.'
        }
        if (($runtimeEnvProvenance -eq 'installer-generated' -and $runtimeEnvSource -cne 'config.example') -or
            ($runtimeEnvProvenance -eq 'installer-preserved' -and $runtimeEnvSource -notin @('existing-install', 'candidate'))) {
            throw 'Runtime .env provenance contract source does not match its provenance.'
        }
        $runtimeEnvExpectedSha256 = $runtimeEnvExpectedSha256.ToLowerInvariant()
        $runtimeEnvContractApplied = $true
    }
    $declaredFileCount = -1
    if (-not ($manifest.PSObject.Properties.Name -contains 'files') -or $null -eq $manifest.files -or -not ($manifest.PSObject.Properties.Name -contains 'file_count') -or -not [int]::TryParse([string]$manifest.file_count, [ref]$declaredFileCount) -or $declaredFileCount -lt 0) {
        throw 'Source manifest must contain a non-negative integer file_count and a files list.'
    }
    $runtimeDirectoryNames = @(
        '.git', '.venv', '.venv313', '__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.tox',
        'spool', 'receipts', 'fixtures', 'uploads', 'output', 'logs', 'cache', 'backups', 'proofs', 'evidence',
        'runtime', 'queue', 'documents', 'customer', 'projects', 'sessions', 'ocr', 'renders', 'env',
        'source-bundle', 'artifacts', 'archives', 'staging', 'temp', 'tmp', 'build', 'dist'
    ) | ForEach-Object { $_.ToLowerInvariant() }
    $generatedRootManifestNames = @('manifest.json', 'source-manifest.json', 'source_bundle_manifest.json')
    $runtimeSuffixes = @('.pyc', '.pyo', '.pyd', '.pid', '.db', '.sqlite', '.sqlite3', '.log', '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z')
    $entries = @($manifest.files)
    if ($entries.Count -gt $script:MaxManifestEntries) {
        throw "Source manifest contains more than the bounded limit of $script:MaxManifestEntries entries."
    }
    foreach ($entry in $entries) {
        if ($null -eq $entry) {
            $mismatches += [pscustomobject]@{ path = '<null>'; reason = 'invalid file entry' }
            continue
        }
        $relative = [string]$entry.path
        $entrySize = -1
        $entryHash = [string]$entry.sha256
        if (-not [int64]::TryParse([string]$entry.size, [ref]$entrySize) -or $entrySize -lt 0 -or $entryHash -notmatch '^[0-9a-fA-F]{64}$') {
            $mismatches += [pscustomobject]@{ path = $relative; reason = 'invalid file size or sha256' }
            continue
        }
        $relativeWindows = Assert-WindowsSafeSourceRelativePath -RelativePath $relative
        $segments = @($relativeWindows.Split([char]92)) # '\\')
        $lowerSegments = @($segments | ForEach-Object { $_.ToLowerInvariant() })
        if (Test-ProhibitedPrivateSourceMember -RelativePath $relativeWindows) {
            $mismatches += [pscustomobject]@{ path = $relative; reason = 'private source member is not allowed' }
            continue
        }
        if (Test-ProhibitedSourceMember -RelativePath $relativeWindows) {
            $mismatches += [pscustomobject]@{ path = $relative; reason = 'prohibited source member is not allowed' }
            continue
        }
        $runtimeEntry = @($lowerSegments | Where-Object { $runtimeDirectoryNames -contains $_ -or $_.StartsWith('.hwpx-install') }).Count -gt 0
        $runtimeEntry = $runtimeEntry -or $lowerSegments[-1].StartsWith('.env') -or ($segments.Count -eq 1 -and $generatedRootManifestNames -contains $lowerSegments[0]) -or $runtimeSuffixes -contains ([IO.Path]::GetExtension($relativeWindows).ToLowerInvariant())
        if ($runtimeEntry) {
            $mismatches += [pscustomobject]@{ path = $relative; reason = 'runtime source member is not allowed' }
            continue
        }
        $key = ($segments -join [char]47).ToLowerInvariant() # '\').ToLowerInvariant()
        $unsafeSegments = @($segments | Where-Object { $_ -in @('', '.') })
        if ([string]::IsNullOrWhiteSpace($relative) -or $relative.IndexOf([char]0) -ge 0 -or [System.IO.Path]::IsPathRooted($relativeWindows) -or $unsafeSegments.Count -gt 0 -or ($segments -contains '..')) { # '(^|\\)\.\.(\\|$)') {
            $mismatches += [pscustomobject]@{ path = $relative; reason = 'unsafe path' }
            continue
        }
        if ($seen.ContainsKey($key)) {
            $mismatches += [pscustomobject]@{ path = $relative; reason = 'duplicate path' }
            continue
        }
        $seen[$key] = $true
        try {
            $candidate = Assert-NoReparseSourcePath -Root $root -RelativePath $relativeWindows
            $resolvedCandidate = [System.IO.Path]::GetFullPath($candidate)
            $rootPrefix = $root.TrimEnd([char]92) + [char]92
            if (-not ($resolvedCandidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or $resolvedCandidate -eq $root)) {
                throw 'path escapes source root'
            }
            $item = Get-Item -LiteralPath $candidate -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'reparse-point source member is not allowed'
            }
            $actualHash = Get-Sha256Hex -Path $candidate
            $actualSize = [int64]$item.Length
            if ($actualHash -ne $entryHash.ToLowerInvariant() -or $actualSize -ne $entrySize) {
                $mismatches += [pscustomobject]@{
                    path = $relative
                    reason = 'hash-or-size-mismatch'
                    expected_sha256 = $entryHash
                    actual_sha256 = $actualHash
                    expected_size = $entrySize
                    actual_size = $actualSize
                }
            }
            if ($null -ne $independentGit) {
                $gitMember = Get-GitSourceMemberIdentity -SourceRoot $root -RelativePath $relativeWindows -Commit ([string]$independentGit.commit)
                if (-not $gitMember.matched) {
                    $mismatches += [pscustomobject]@{
                        path = $relative
                        reason = 'source bytes do not match the independently read Git tree blob'
                        expected_git_blob = $gitMember.expected_blob
                        actual_git_blob = $gitMember.actual_blob
                    }
                }
                if (-not $gitMember.mode_matched) {
                    $mismatches += [pscustomobject]@{
                        path = $relative
                        reason = 'source mode does not match the independently read Git tree mode'
                        expected_git_mode = $gitMember.expected_mode
                        actual_git_mode = $gitMember.actual_mode
                    }
                }
                if ($entry.PSObject.Properties.Name -contains 'git_mode' -and [string]$entry.git_mode -cne [string]$gitMember.expected_mode) {
                    $mismatches += [pscustomobject]@{
                        path = $relative
                        reason = 'manifest Git mode does not match the independently read Git tree mode'
                        manifest_git_mode = [string]$entry.git_mode
                        expected_git_mode = $gitMember.expected_mode
                    }
                }
            }
        }
        catch {
            $mismatches += [pscustomobject]@{ path = $relative; reason = $_.Exception.Message }
        }
    }
    $manifestCanonical = Get-CanonicalPath -Path $manifestFile -RequireExisting
    $archiveName = if ($manifest.PSObject.Properties.Name -contains 'archive') { [string]$manifest.archive } else { '' }
    foreach ($directory in @(Get-ChildItem -LiteralPath $root -Recurse -Directory -Force -ErrorAction Stop)) {
        if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) { continue }
        $relativeDirectory = $directory.FullName.Substring($root.Length).TrimStart([char]92, [char]47)
        $ignoredRuntimePath = $false
        foreach ($part in @($relativeDirectory.Split([char]92))) {
            $partLower = $part.ToLowerInvariant()
            if ($runtimeDirectoryNames -contains $partLower -or $partLower.StartsWith('.hwpx-install')) {
                $ignoredRuntimePath = $true
                break
            }
        }
        if (-not $ignoredRuntimePath) { throw "Reparse-point source directory is not allowed: $relativeDirectory" }
    }
    foreach ($item in @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction Stop)) {
        $relativeActual = $item.FullName.Substring($root.Length).TrimStart([char]92, [char]47)
        $actualParts = @($relativeActual.Split([char]92))
        $ignoredRuntimePath = $false
        foreach ($part in $actualParts) {
            $partLower = $part.ToLowerInvariant()
            if ($runtimeDirectoryNames -contains $partLower -or $partLower.StartsWith('.hwpx-install')) {
                $ignoredRuntimePath = $true
                break
            }
        }
        # Runtime trees can contain legitimate dependency names such as
        # requests\cookies.py. Exclude the whole runtime path before applying
        # private-source markers, while keeping those markers strict for source.
        if ($ignoredRuntimePath) { continue }
        if ($runtimeEnvContractApplied -and $relativeActual -ieq $runtimeEnvExpectedPath) {
            $runtimeEnvObserved = $true
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $mismatches += [pscustomobject]@{ path = $relativeActual; reason = 'runtime .env reparse-point is not allowed' }
                continue
            }
            try {
                $actualRuntimeEnvSize = [int64]$item.Length
                $actualRuntimeEnvSha256 = Get-Sha256Hex -Path $item.FullName
                if ($actualRuntimeEnvSize -ne $runtimeEnvExpectedSize -or $actualRuntimeEnvSha256 -cne $runtimeEnvExpectedSha256) {
                    $mismatches += [pscustomobject]@{
                        path = $relativeActual
                        reason = 'installer-generated runtime .env provenance hash-or-size mismatch'
                        expected_sha256 = $runtimeEnvExpectedSha256
                        actual_sha256 = $actualRuntimeEnvSha256
                        expected_size = $runtimeEnvExpectedSize
                        actual_size = $actualRuntimeEnvSize
                    }
                }
            }
            catch {
                $mismatches += [pscustomobject]@{ path = $relativeActual; reason = $_.Exception.Message }
            }
            continue
        }
        $runtimeEnvMarker = $item.Name.ToLowerInvariant().StartsWith('.env')
        if ($runtimeEnvMarker) {
            $mismatches += [pscustomobject]@{
                path = $relativeActual
                reason = if ($runtimeEnvContractApplied) { 'runtime .env is outside the bound provenance path' } else { 'runtime .env requires explicit installer provenance' }
            }
            continue
        }
        if ($item.Name.ToLowerInvariant().StartsWith('.hwpx-install') -or $item.Extension.ToLowerInvariant() -in $runtimeSuffixes) { continue }
        if ($actualParts.Count -eq 1 -and $generatedRootManifestNames -contains $item.Name.ToLowerInvariant()) { continue }
        if ($item.FullName -eq $manifestCanonical -or ($archiveName -and $relativeActual.Replace([char]92, '/') -eq $archiveName.Replace([char]92, '/'))) { continue }
        if (Test-ProhibitedPrivateSourceMember -RelativePath $relativeActual) {
            $mismatches += [pscustomobject]@{ path = $relativeActual; reason = 'private source member is not allowed' }
            continue
        }
        if ($actualParts.Count -eq 1 -and (Test-ProhibitedSourceMember -RelativePath $relativeActual)) {
            # Root-level generated manifests are excluded from the source
            # closure, while nested command manifests remain source code.
            continue
        }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            $mismatches += [pscustomobject]@{ path = $relativeActual; reason = 'reparse-point source member is not allowed' }
            continue
        }
        $actualKey = $relativeActual.Replace([char]92, '/').ToLowerInvariant()
        if (-not $seen.ContainsKey($actualKey)) {
            $mismatches += [pscustomobject]@{ path = $relativeActual; reason = 'unlisted source member' }
        }
    }
    if ($runtimeEnvContractApplied -and -not $runtimeEnvObserved) {
        $mismatches += [pscustomobject]@{ path = $runtimeEnvExpectedPath; reason = 'installer-generated runtime .env is missing' }
    }
    if ($declaredFileCount -ne $entries.Count) {
        $mismatches += [pscustomobject]@{ path = '<manifest>'; reason = 'file-count-mismatch'; expected = $declaredFileCount; actual = $entries.Count }
    }
    return [pscustomobject]@{
        ok = ($mismatches.Count -eq 0)
        manifest = $manifest
        path = $manifestFile
        manifest_sha256 = $manifestHash
        source_root = $root
        identity_source = $identitySource
        identity_verified = $identityVerified
        identity_binding = [pscustomobject]@{
            independent_git = ($null -ne $independentGit)
            external_identity = ($externalIdentityCount -eq 3)
            verified = $identityBindingVerified
        }
        file_count = $entries.Count
        mismatch_count = $mismatches.Count
        mismatches = @($mismatches)
        runtime_env_contract_applied = [bool]$runtimeEnvContractApplied
        runtime_env_contract = $runtimeEnvContract
    }
}

function Resolve-WindowsPrincipalIdentity {
    [CmdletBinding()]
    param([AllowNull()][string]$Principal)

    if ([string]::IsNullOrWhiteSpace($Principal)) { return $null }
    $value = $Principal.Trim()
    $separator = [string][char]92
    $machineName = [string]$env:COMPUTERNAME
    if ([string]::IsNullOrWhiteSpace($machineName)) {
        $machineName = [string][Environment]::MachineName
    }

    # Get-ScheduledTask can return a local interactive account as `owner`,
    # while registration/readback uses `COMPUTERNAME\owner`. Resolve both
    # spellings to a SID before falling back to a normalized account name.
    $candidates = @($value)
    if ($value.StartsWith('.' + $separator, [StringComparison]::Ordinal)) {
        if (-not [string]::IsNullOrWhiteSpace($machineName)) {
            $candidates += $machineName + $separator + $value.Substring(2)
        }
    }
    elseif ($value.IndexOf([char]92) -lt 0 -and -not [string]::IsNullOrWhiteSpace($machineName)) {
        $candidates += $machineName + $separator + $value
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        try {
            $account = New-Object System.Security.Principal.NTAccount([string]$candidate)
            $sid = $account.Translate([System.Security.Principal.SecurityIdentifier])
            if ($sid) {
                return [pscustomobject]@{
                    sid = [string]$sid.Value
                    account = ([string]$candidate).ToUpperInvariant()
                }
            }
        }
        catch {
            # Try the next spelling; unresolved accounts remain fail-closed.
        }
    }

    try {
        $sid = New-Object System.Security.Principal.SecurityIdentifier($value)
        return [pscustomobject]@{
            sid = [string]$sid.Value
            account = $null
        }
    }
    catch {
        # Preserve a deterministic account fallback when SID translation is
        # unavailable (for example, an offline domain account).
    }

    $normalized = $value
    if ($value.StartsWith('.' + $separator, [StringComparison]::Ordinal)) {
        if (-not [string]::IsNullOrWhiteSpace($machineName)) {
            $normalized = $machineName + $separator + $value.Substring(2)
        }
    }
    elseif ($value.IndexOf([char]92) -lt 0 -and -not [string]::IsNullOrWhiteSpace($machineName)) {
        $normalized = $machineName + $separator + $value
    }
    return [pscustomobject]@{
        sid = $null
        account = $normalized.ToUpperInvariant()
    }
}

function Test-WindowsPrincipalEquivalent {
    [CmdletBinding()]
    param(
        [AllowNull()][string]$Actual,
        [AllowNull()][string]$Expected
    )

    if ([string]::IsNullOrWhiteSpace($Expected)) { return $true }
    if ([string]::IsNullOrWhiteSpace($Actual)) { return $false }
    $actualIdentity = Resolve-WindowsPrincipalIdentity -Principal $Actual
    $expectedIdentity = Resolve-WindowsPrincipalIdentity -Principal $Expected
    if ($actualIdentity.sid -and $expectedIdentity.sid) {
        return ([string]$actualIdentity.sid -ieq [string]$expectedIdentity.sid)
    }
    return ([string]$actualIdentity.account -ieq [string]$expectedIdentity.account)
}

function Test-ScheduledTaskLogonTypeEquivalent {
    [CmdletBinding()]
    param(
        [AllowNull()][string]$Actual,
        [AllowNull()][string]$Expected
    )

    if ([string]$Expected -ieq 'Interactive' -and [string]$Actual -ieq 'InteractiveToken') {
        # Windows PowerShell 5.1 may persist the Interactive registration
        # enum as InteractiveToken in the task XML/readback object.
        return $true
    }
    return ([string]$Actual -ieq [string]$Expected)
}

function Test-ScheduledTaskRunLevelEquivalent {
    [CmdletBinding()]
    param(
        [AllowNull()][string]$Actual,
        [AllowNull()][string]$Expected
    )

    if ([string]$Expected -ieq 'Limited' -and [string]::IsNullOrWhiteSpace($Actual)) {
        # Task Scheduler omits the default Limited RunLevel in PS5.1 XML.
        return $true
    }
    return ([string]$Actual -ieq [string]$Expected)
}

function Test-CanonicalTaskSettings {
    [CmdletBinding()]
    param([AllowNull()][object]$Identity)
    try {
        if ($null -eq $Identity) { return $false }
        $settings = Get-OptionalPropertyValue -Object $Identity -Name 'settings'
        if ($null -eq $settings) { return $false }
        $startWhenAvailable = [string](Get-OptionalPropertyValue -Object $settings -Name 'StartWhenAvailable')
        if ($startWhenAvailable -ine 'true') { return $false }
        $enabledProperty = $Identity.PSObject.Properties['enabled']
        if ($null -eq $enabledProperty -or -not [bool]$enabledProperty.Value) { return $false }
        foreach ($settingName in @('DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries', 'RunOnlyIfNetworkAvailable', 'Hidden')) {
            $value = [string](Get-OptionalPropertyValue -Object $settings -Name $settingName)
            if ($settingName -in @('DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries')) {
                if ($value -ine 'false') { return $false }
            }
            elseif (-not [string]::IsNullOrWhiteSpace($value) -and $value -ine 'false') { return $false }
        }
        $multipleInstances = [string](Get-OptionalPropertyValue -Object $settings -Name 'MultipleInstancesPolicy')
        if (-not [string]::IsNullOrWhiteSpace($multipleInstances) -and $multipleInstances -ine 'IgnoreNew') { return $false }
        $allowHardTerminate = [string](Get-OptionalPropertyValue -Object $settings -Name 'AllowHardTerminate')
        if (-not [string]::IsNullOrWhiteSpace($allowHardTerminate) -and $allowHardTerminate -ine 'true') { return $false }
        $executionTimeLimit = [string](Get-OptionalPropertyValue -Object $settings -Name 'ExecutionTimeLimit')
        if ([string]::IsNullOrWhiteSpace($executionTimeLimit) -or $executionTimeLimit -notin @('PT0S', 'PT0M', 'P0D', '00:00:00')) { return $false }
        $restartCount = [string](Get-OptionalPropertyValue -Object $settings -Name 'RestartCount')
        if ($restartCount -ne '3') { return $false }
        $restartInterval = [string](Get-OptionalPropertyValue -Object $settings -Name 'RestartInterval')
        if ($restartInterval -notin @('PT5M', '00:05:00')) { return $false }
        return $true
    }
    catch {
        return $false
    }
}

function Assert-SafeScheduledTaskName {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$TaskName)
    if ([string]::IsNullOrWhiteSpace($TaskName) -or $TaskName.Length -gt 256) {
        throw 'Scheduled task name must be non-empty and at most 256 characters.'
    }
    if ($TaskName -ne $TaskName.Trim() -or $TaskName -match '[\x00-\x1f]' -or $TaskName -in @('.', '..')) {
        throw "Scheduled task name is not a canonical exact identifier: $TaskName"
    }
    if ($TaskName -match '[\*\?\[\]]' -or $TaskName.Contains([char]92) -or $TaskName.Contains('/')) {
        throw "Scheduled task name contains wildcard or path metacharacters: $TaskName"
    }
    return $TaskName
}

function Assert-CanonicalScheduledTaskPath {
    [CmdletBinding()]
    param([string]$TaskPath = $script:DefaultTaskPath)
    if ([string]::IsNullOrWhiteSpace($TaskPath)) { $TaskPath = $script:DefaultTaskPath }
    if ($TaskPath.Length -gt 256 -or -not $TaskPath.StartsWith([char]92) -or -not $TaskPath.EndsWith([char]92) -or $TaskPath -eq '\\' -or $TaskPath.Contains('/') -or $TaskPath -match '[\x00-\x1f]' -or $TaskPath -match '[\*\?\[\]]') {
        throw "Scheduled task path is not canonical: $TaskPath"
    }
    if ($TaskPath -ne $script:DefaultTaskPath) {
        $pathBody = $TaskPath.Substring(1, $TaskPath.Length - 2)
        $segments = @($pathBody.Split([char[]]@([char]92)))
        if ($segments.Count -eq 0) {
            throw "Scheduled task path is not canonical: $TaskPath"
        }
        foreach ($segment in $segments) {
            if ([string]::IsNullOrWhiteSpace($segment) -or $segment -ne $segment.Trim() -or $segment -in @('.', '..') -or $segment -match '[\x00-\x1f<>:"/\\|?*]') {
                throw "Scheduled task path is not canonical: $TaskPath"
            }
        }
    }
    return $TaskPath
}

function Get-ScheduledTaskExact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [string]$TaskPath = $script:DefaultTaskPath,
        [switch]$AllowMissing
    )
    $safeName = Assert-SafeScheduledTaskName -TaskName $TaskName
    $safePath = Assert-CanonicalScheduledTaskPath -TaskPath $TaskPath
    try {
        $matches = @(Get-ScheduledTask -TaskName $safeName -TaskPath $safePath -ErrorAction Stop)
    }
    catch {
        if ($AllowMissing -and [string]$_.CategoryInfo.Category -eq 'ObjectNotFound') { return $null }
        throw "Could not read scheduled task '$safePath$safeName': $($_.Exception.Message)"
    }
    if ($matches.Count -ne 1) {
        throw "Scheduled task name/path did not resolve to exactly one task: '$safePath$safeName' (count=$($matches.Count))"
    }
    $task = $matches[0]
    if ([string]$task.TaskName -cne $safeName -or [string]$task.TaskPath -cne $safePath) {
        throw "Scheduled task identity changed during exact lookup: '$safePath$safeName'"
    }
    return $task
}

function Get-ScheduledTaskIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [string]$TaskPath = $script:DefaultTaskPath
    )

    $safePath = Assert-CanonicalScheduledTaskPath -TaskPath $TaskPath
    $task = Get-ScheduledTaskExact -TaskName $TaskName -TaskPath $safePath -AllowMissing
    if ($null -eq $task) {
        return [pscustomobject]@{ exists = $false; task_name = $TaskName; task_path = $safePath }
    }
    $actions = @($task.Actions)
    $action = if ($actions.Count -gt 0) { $actions[0] } else { $null }
    $taskXml = try { [string](Export-ScheduledTask -TaskName $TaskName -TaskPath $safePath -ErrorAction Stop) } catch { $null }
    if ($null -ne $taskXml -and $taskXml.Length -gt $script:MaxTaskXmlChars) {
        throw "Scheduled task XML exceeds the bounded size limit of $script:MaxTaskXmlChars characters: $TaskName"
    }
    $xml = $null
    $triggerTypes = @()
    $triggerUsers = @()
    $logonType = $null
    $runLevel = $null
    $settings = [ordered]@{}
    $actionType = $null
    $xmlActionCount = 0
    $xmlExecActionCount = 0
    $xmlActionNodes = @()
    $principalNode = $null
    if (-not [string]::IsNullOrWhiteSpace($taskXml)) {
        try {
            $xml = [xml]$taskXml
            $triggerNodes = @($xml.SelectNodes("//*[local-name()='Triggers']/*"))
            $triggerTypes = @($triggerNodes | ForEach-Object { [string]$_.LocalName })
            $triggerUsers = @($triggerNodes | ForEach-Object {
                $userNode = $_.SelectSingleNode(".//*[local-name()='UserId']")
                if ($userNode) { [string]$userNode.InnerText }
            } | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
            $principalNode = $xml.SelectSingleNode("//*[local-name()='Principals']/*[local-name()='Principal']")
            if ($principalNode) {
                $logonNode = $principalNode.SelectSingleNode("./*[local-name()='LogonType']")
                $levelNode = $principalNode.SelectSingleNode("./*[local-name()='RunLevel']")
                if ($logonNode) { $logonType = [string]$logonNode.InnerText }
                if ($levelNode) { $runLevel = [string]$levelNode.InnerText }
            }
            $xmlActionNodes = @($xml.SelectNodes("//*[local-name()='Actions']/*"))
            $xmlActionCount = $xmlActionNodes.Count
            $xmlExecActionCount = @($xmlActionNodes | Where-Object { [string]$_.LocalName -eq 'Exec' }).Count
            if ($xmlActionCount -eq 1) { $actionType = [string]$xmlActionNodes[0].LocalName }
            $settingsNode = $xml.SelectSingleNode("//*[local-name()='Settings']")
            $restartNode = if ($settingsNode) { $settingsNode.SelectSingleNode("./*[local-name()='RestartOnFailure']") } else { $null }
            foreach ($settingName in @(
                'MultipleInstancesPolicy', 'DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries',
                'AllowHardTerminate', 'StartWhenAvailable', 'RunOnlyIfNetworkAvailable',
                'IdleSettings', 'Enabled', 'Hidden', 'ExecutionTimeLimit', 'RestartCount', 'RestartInterval', 'Priority'
            )) {
                $settingNode = if ($settingsNode) { $settingsNode.SelectSingleNode("./*[local-name()='$settingName']") } else { $null }
                $settings[$settingName] = if ($settingNode) { [string]$settingNode.InnerText } else { $null }
            }
            $restartCountNode = if ($restartNode) { $restartNode.SelectSingleNode("./*[local-name()='Count']") } else { $null }
            $restartIntervalNode = if ($restartNode) { $restartNode.SelectSingleNode("./*[local-name()='Interval']") } else { $null }
            $settings['RestartCount'] = if ($restartCountNode) { [string]$restartCountNode.InnerText } else { $null }
            $settings['RestartInterval'] = if ($restartIntervalNode) { [string]$restartIntervalNode.InnerText } else { $null }
        }
        catch {
            throw "Scheduled task XML could not be parsed: $TaskName"
        }
    }
    $taskIdentityHash = $null
    if (-not [string]::IsNullOrWhiteSpace($taskXml)) {
        $identitySha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $identityBytes = [System.Text.Encoding]::UTF8.GetBytes($taskXml)
            $taskIdentityHash = ([System.BitConverter]::ToString($identitySha.ComputeHash($identityBytes))).Replace('-', '').ToLowerInvariant()
        }
        finally { $identitySha.Dispose() }
    }
    $principal = if ($task.Principal) { [string]$task.Principal.UserId } else { $null }
    if ([string]::IsNullOrWhiteSpace($principal) -and $principalNode) {
        $principalNodeValue = $principalNode.SelectSingleNode("./*[local-name()='UserId']")
        if ($principalNodeValue) { $principal = [string]$principalNodeValue.InnerText }
    }
    $enabled = $null
    try {
        if ($null -ne $task.Settings -and $null -ne $task.Settings.Enabled) {
            $enabled = [bool]$task.Settings.Enabled
        }
    }
    catch { $enabled = $null }
    if ($null -eq $enabled) {
        $persistedEnabled = [string]$settings['Enabled']
        # Task Scheduler treats an omitted Enabled element as enabled.
        $enabled = [string]::IsNullOrWhiteSpace($persistedEnabled) -or $persistedEnabled -ieq 'true'
    }
    $persistedLogonType = $logonType
    $persistedRunLevel = $runLevel
    # Windows PowerShell 5.1 persists the Interactive enum as
    # InteractiveToken and omits the default Limited RunLevel. Keep the raw
    # values for diagnostics while exposing the canonical contract values to
    # installer, verifier, and writer callers.
    $normalizedLogonType = if ([string]$logonType -ieq 'InteractiveToken') { 'Interactive' } else { $logonType }
    $normalizedRunLevel = if ($null -ne $xml -and [string]::IsNullOrWhiteSpace($runLevel)) { 'Limited' } else { $runLevel }
    return [pscustomobject]@{
        exists = $true
        task_name = $TaskName
        task_path = $safePath
        state = [string]$task.State
        principal = $principal
        enabled = [bool]$enabled
        execute = if ($action) { [string]$action.Execute } else { $null }
        arguments = if ($action) { [string]$action.Arguments } else { $null }
        working_directory = if ($action) { [string]$action.WorkingDirectory } else { $null }
        action_type = $actionType
        action_count = [int]$actions.Count
        xml_action_count = [int]$xmlActionCount
        xml_exec_action_count = [int]$xmlExecActionCount
        actions = @($actions | ForEach-Object {
            [pscustomobject]@{
                action_type = if ($_.PSObject.Properties.Name -contains 'Execute') { 'Exec' } else { $null }
                execute = if ($_.PSObject.Properties.Name -contains 'Execute') { [string]$_.Execute } else { $null }
                arguments = if ($_.PSObject.Properties.Name -contains 'Arguments') { [string]$_.Arguments } else { $null }
                working_directory = if ($_.PSObject.Properties.Name -contains 'WorkingDirectory') { [string]$_.WorkingDirectory } else { $null }
            }
        })
        trigger_type = if ($triggerTypes.Count -eq 1) { [string]$triggerTypes[0] } else { @($triggerTypes) }
        trigger_types = @($triggerTypes)
        trigger_user = if ($triggerUsers.Count -eq 1) { [string]$triggerUsers[0] } else { @($triggerUsers) }
        logon_type = $normalizedLogonType
        run_level = $normalizedRunLevel
        persisted_logon_type = $persistedLogonType
        persisted_run_level = $persistedRunLevel
        settings = [pscustomobject]$settings
        multiple_instances_policy = $settings['MultipleInstancesPolicy']
        start_when_available = $settings['StartWhenAvailable']
        execution_time_limit = $settings['ExecutionTimeLimit']
        restart_count = $settings['RestartCount']
        restart_interval = $settings['RestartInterval']
        trigger_semantics = if ($triggerTypes -contains 'LogonTrigger') { 'AtLogOn' } else { $null }
        task_identity_sha256 = $taskIdentityHash
        task_identity_hash = $taskIdentityHash
        task_xml_sha256 = $taskIdentityHash
        xml = $taskXml
    }
}

function Test-CanonicalPathWithinRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )
    # Reject symlink/reparse components before any canonicalization or
    # nearest-existing-ancestor fallback can resolve through them.
    try {
        Assert-NoReparsePath -Path $Root | Out-Null
        Assert-NoReparsePath -Path $Path | Out-Null
    }
    catch { return $false }
    $canonicalRoot = $null
    try {
        # The intended InstallRoot may not exist yet. Resolve its absolute
        # boundary without requiring creation so a receipt cannot create a
        # child path that later cleanup would own.
        $canonicalRoot = Get-CanonicalPath -Path $Root
    }
    catch { return $false }
    try {
        $canonicalPath = Get-CanonicalPath -Path $Path -RequireExisting
    }
    catch {
        # Receipt destinations are commonly new files. Resolve the nearest
        # existing ancestor before appending the non-existent suffix so a
        # path inside the preserved root cannot bypass the boundary check.
        try {
            $unresolved = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path))
            $suffix = New-Object System.Collections.Generic.List[string]
            while (-not (Test-Path -LiteralPath $unresolved)) {
                $leaf = Split-Path -Leaf $unresolved
                if ([string]::IsNullOrWhiteSpace($leaf)) { return $false }
                $suffix.Insert(0, $leaf)
                $parent = Split-Path -Parent $unresolved
                if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $unresolved) { return $false }
                $unresolved = $parent
            }
            $canonicalPath = Get-CanonicalPath -Path $unresolved -RequireExisting
            foreach ($part in $suffix) { $canonicalPath = Join-Path $canonicalPath $part }
        }
        catch { return $false }
    }
    # Boundary-safe root matching prevents candidate-old from matching candidate.
    $rootBoundaryPrefix = $canonicalRoot.TrimEnd([char[]]@([char]92, [char]47)) + [char]92
    return ($canonicalPath -eq $canonicalRoot -or $canonicalPath.StartsWith($rootBoundaryPrefix, [StringComparison]::OrdinalIgnoreCase))
}

function Test-CommandLineModuleToken {
    [CmdletBinding()]
    param(
        [AllowNull()][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$ModuleName
    )
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    $pattern = '(?i)(?<![A-Za-z0-9_.-])' + [regex]::Escape($ModuleName) + '(?![A-Za-z0-9_.-])'
    return [regex]::IsMatch($CommandLine, $pattern)
}

function Test-CommandLinePathToken {
    [CmdletBinding()]
    param(
        [AllowNull()][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$Path
    )
    if ([string]::IsNullOrWhiteSpace($CommandLine) -or [string]::IsNullOrWhiteSpace($Path)) { return $false }
    $normalizedPath = $Path.Trim().Trim('"').Replace('/', '\')
    $pattern = '(?i)(?<![A-Za-z0-9_.-])' + [regex]::Escape($normalizedPath) + '(?![A-Za-z0-9_.-])'
    return [regex]::IsMatch($CommandLine, $pattern)
}

function Get-OptionalPropertyValue {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary] -and $Object.Contains($Name)) {
        return $Object[$Name]
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-TextSha256 {
    param([AllowNull()][string]$Value)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Value)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-ExecutableVersion {
    [CmdletBinding()]
    param([AllowNull()][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        $version = [string]$item.VersionInfo.ProductVersion
        if ([string]::IsNullOrWhiteSpace($version)) {
            $version = [string]$item.VersionInfo.FileVersion
        }
        if ([string]::IsNullOrWhiteSpace($version)) { return $null }
        return $version.Trim()
    }
    catch {
        return $null
    }
}

function Get-InterpreterVersionKey {
    [CmdletBinding()]
    param([AllowNull()][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $match = [regex]::Match($Value, '(?<![0-9])([0-9]+)\.([0-9]+)(?![0-9])')
    if (-not $match.Success) { return $null }
    return "{0}.{1}" -f $match.Groups[1].Value, $match.Groups[2].Value
}

function Get-RegisteredStorePythonImage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Home,
        [Parameter(Mandatory = $true)][string]$ConfiguredExecutable,
        [Parameter(Mandatory = $true)][string]$VersionKey
    )
    try {
        # Store execution aliases are not the process image reported by CIM.
        # Bind only an exact current-user, healthy Store package registration;
        # a matching filename/version or arbitrary WindowsApps prefix is not proof.
        if ($VersionKey -notmatch '^[0-9]+\.[0-9]+$') { return $null }
        $aliasRoot = Get-CanonicalPath -Path (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Microsoft\WindowsApps') -RequireExisting
        $homePath = Get-CanonicalPath -Path $Home -RequireExisting
        if ((Get-CanonicalPath -Path (Split-Path -Parent $homePath) -RequireExisting) -cne $aliasRoot) { return $null }
        $configured = Get-CanonicalPath -Path $ConfiguredExecutable -RequireExisting
        if ((Get-CanonicalPath -Path (Split-Path -Parent $configured) -RequireExisting) -cne $homePath) { return $null }
        $imageName = 'python{0}.exe' -f $VersionKey
        if ([IO.Path]::GetFileName($configured) -notin @('python.exe', $imageName)) { return $null }
        $packageName = 'PythonSoftwareFoundation.Python.{0}' -f $VersionKey
        $family = [IO.Path]::GetFileName($homePath)
        $packages = @(Get-AppxPackage -Name $packageName -ErrorAction Stop)
        if ($packages.Count -ne 1) { return $null }
        $package = $packages[0]
        if ([string]$package.Name -cne $packageName -or [string]$package.PackageFamilyName -cne $family -or
            [string]$package.Status -cne 'Ok' -or [string]$package.SignatureKind -cne 'Store') { return $null }
        $location = Get-CanonicalPath -Path ([string]$package.InstallLocation) -RequireExisting
        Assert-NoReparsePath -Path $location | Out-Null
        $image = Join-Path $location $imageName
        Assert-NoReparsePath -Path $image | Out-Null
        return Get-CanonicalPath -Path $image -RequireExisting
    }
    catch { return $null }
}

function Get-VenvInterpreterMetadata {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$ExpectedPythonPath
    )
    try {
        $root = Get-CanonicalPath -Path $RootPath -RequireExisting
        $python = Get-CanonicalPath -Path $ExpectedPythonPath -RequireExisting
        $venvScripts = Split-Path -Parent $python
        $venvRoot = Split-Path -Parent $venvScripts
        $configPath = Join-Path $venvRoot 'pyvenv.cfg'
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { return $null }
        $capture = Read-BoundedText -Path $configPath -MaxChars 4096
        if ($capture.truncated) { return $null }
        $home = $null
        $version = $null
        $configuredExecutable = $null
        foreach ($line in @([string]$capture.text -split "`r?`n")) {
            if ([string]$line -match '^\s*home\s*=\s*(.*?)\s*$') { $home = [string]$matches[1] }
            elseif ([string]$line -match '^\s*version\s*=\s*(.*?)\s*$') { $version = [string]$matches[1] }
            elseif ([string]$line -match '^\s*executable\s*=\s*(.*?)\s*$') { $configuredExecutable = [string]$matches[1] }
        }
        if ([string]::IsNullOrWhiteSpace($home) -or [string]::IsNullOrWhiteSpace($version)) { return $null }
        $canonicalHome = Get-CanonicalPath -Path $home -RequireExisting
        $baseLeaf = [IO.Path]::GetFileName($python)
        $baseNames = @($baseLeaf)
        $versionedBaseName = $null
        $versionMatch = [regex]::Match($version.Trim(), '(?<![0-9])([0-9]+)\.([0-9]+)(?![0-9])')
        if ($versionMatch.Success) {
            $versionedBaseName = 'python{0}.{1}.exe' -f $versionMatch.Groups[1].Value, $versionMatch.Groups[2].Value
            if ($baseNames -notcontains $versionedBaseName) { $baseNames += $versionedBaseName }
        }
        $baseExecutables = @()
        if (-not [string]::IsNullOrWhiteSpace($configuredExecutable)) {
            try {
                $configuredCanonical = Get-CanonicalPath -Path $configuredExecutable -RequireExisting
                $configuredLeaf = [IO.Path]::GetFileName($configuredCanonical)
                if ($configuredLeaf -ieq $baseLeaf -or $configuredLeaf -ieq $versionedBaseName) {
                    $baseExecutables += $configuredCanonical
                }
            }
            catch { }
        }
        foreach ($baseName in $baseNames) {
            $baseCandidate = Join-Path $canonicalHome $baseName
            if (-not (Test-Path -LiteralPath $baseCandidate -PathType Leaf)) { continue }
            try {
                $candidateCanonical = Get-CanonicalPath -Path $baseCandidate -RequireExisting
                if (@($baseExecutables | Where-Object { [string]$_ -ceq $candidateCanonical }).Count -eq 0) {
                    $baseExecutables += $candidateCanonical
                }
            }
            catch { }
        }
        if (-not [string]::IsNullOrWhiteSpace($configuredExecutable) -and $versionMatch.Success) {
            $storeImage = Get-RegisteredStorePythonImage -Home $canonicalHome -ConfiguredExecutable $configuredExecutable -VersionKey (Get-InterpreterVersionKey -Value $version)
            if (-not [string]::IsNullOrWhiteSpace($storeImage) -and $baseExecutables -cnotcontains $storeImage) {
                $baseExecutables += $storeImage
            }
        }
        # Some Windows Store installations deny ordinary metadata access to
        # the package image even though CIM reports that image as the running
        # process executable.  Keep the authenticated home/version binding so
        # Test-CanonicalProcessIdentity can prove that exact versioned image
        # without treating an arbitrary external executable as trusted.
        if ($baseExecutables.Count -eq 0 -and [string]::IsNullOrWhiteSpace($versionedBaseName)) { return $null }
        if (-not (Test-CanonicalPathWithinRoot -Path $python -Root $root)) { return $null }
        return [pscustomobject]@{
            config_path = Get-CanonicalPath -Path $configPath -RequireExisting
            home = $canonicalHome
            base_executable = [string]$baseExecutables[0]
            base_executables = @($baseExecutables)
            versioned_base_name = $versionedBaseName
            configured_executable = [string]$configuredExecutable
            version = $version.Trim()
            version_key = Get-InterpreterVersionKey -Value $version
        }
    }
    catch {
        return $null
    }
}

function Test-CanonicalTaskActionBinding {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Identity,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedPythonPath,
        [AllowNull()][string]$ExpectedArguments,
        [AllowNull()][string]$ExpectedPrincipal,
        [Nullable[int]]$ExpectedApiPort
    )
    if ($null -eq $Identity) { return $false }
    $exists = Get-OptionalPropertyValue -Object $Identity -Name 'exists'
    if ($null -ne $exists -and -not [bool]$exists) { return $false }
    $reportedContract = Get-OptionalPropertyValue -Object $Identity -Name 'contract_ok'
    if ($null -ne $reportedContract -and -not [bool]$reportedContract) { return $false }
    try {
        if ([int](Get-OptionalPropertyValue -Object $Identity -Name 'action_count') -ne 1) { return $false }
        if ([int](Get-OptionalPropertyValue -Object $Identity -Name 'xml_action_count') -ne 1) { return $false }
        if ([int](Get-OptionalPropertyValue -Object $Identity -Name 'xml_exec_action_count') -ne 1) { return $false }
        $root = Get-CanonicalPath -Path $ExpectedRoot -RequireExisting
        $python = Get-CanonicalPath -Path $ExpectedPythonPath -RequireExisting
        $working = Get-CanonicalPath -Path ([string](Get-OptionalPropertyValue -Object $Identity -Name 'working_directory')) -RequireExisting
        $execute = Get-CanonicalPath -Path ([string](Get-OptionalPropertyValue -Object $Identity -Name 'execute')) -RequireExisting
        if ($working -ne $root -or $execute -ne $python) { return $false }
        if (-not [string]::IsNullOrWhiteSpace($ExpectedArguments) -and [string](Get-OptionalPropertyValue -Object $Identity -Name 'arguments') -ne $ExpectedArguments) { return $false }
        if ([string](Get-OptionalPropertyValue -Object $Identity -Name 'action_type') -ne 'Exec') { return $false }
        if ([string](Get-OptionalPropertyValue -Object $Identity -Name 'trigger_type') -ne 'LogonTrigger') { return $false }
        if (-not (Test-ScheduledTaskLogonTypeEquivalent -Actual ([string](Get-OptionalPropertyValue -Object $Identity -Name 'logon_type')) -Expected 'Interactive')) { return $false }
        if (-not (Test-ScheduledTaskRunLevelEquivalent -Actual ([string](Get-OptionalPropertyValue -Object $Identity -Name 'run_level')) -Expected 'Limited')) { return $false }
        if ([string](Get-OptionalPropertyValue -Object $Identity -Name 'start_when_available') -ine 'true') { return $false }
        if (-not (Test-CanonicalTaskSettings -Identity $Identity)) { return $false }
        if ([string]::IsNullOrWhiteSpace([string](Get-OptionalPropertyValue -Object $Identity -Name 'task_identity_hash'))) { return $false }
        if (-not [string]::IsNullOrWhiteSpace($ExpectedPrincipal)) {
            $principal = [string](Get-OptionalPropertyValue -Object $Identity -Name 'principal')
            $triggerUserValue = Get-OptionalPropertyValue -Object $Identity -Name 'trigger_user'
            $triggerUser = if ($triggerUserValue -is [array]) { [string]$triggerUserValue[0] } else { [string]$triggerUserValue }
            if (-not (Test-WindowsPrincipalEquivalent -Actual $principal -Expected $ExpectedPrincipal)) { return $false }
            if (-not (Test-WindowsPrincipalEquivalent -Actual $triggerUser -Expected $ExpectedPrincipal)) { return $false }
        }
        if ($null -ne $ExpectedApiPort) {
            $configured = Get-ConfiguredApiPort -EnvPath (Join-Path $root '.env')
            if ([int]$configured -ne [int]$ExpectedApiPort) { return $false }
        }
        return $true
    }
    catch {
        return $false
    }
}

function Test-CanonicalProcessIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Process,
        [Parameter(Mandatory = $true)][string]$RootPath,
        [string]$ExpectedPythonPath,
        [string[]]$ModuleNames = @('app.api_server', 'app.worker'),
        [AllowNull()][string]$ExpectedArguments,
        [AllowNull()][object]$ExpectedTaskIdentity,
        [AllowNull()][object]$ExpectedListener,
        [Nullable[int]]$ExpectedListenerPort,
        [Nullable[int]]$ExpectedListenerProcessId,
        [AllowNull()][string]$ExpectedInterpreterVersion,
        [Nullable[int]]$ExpectedApiPort,
        [AllowNull()][string]$ExpectedPrincipal,
        [AllowNull()][object]$ExpectedProcessIdentity
    )
    if ($null -eq $Process) { return $false }
    try {
        $root = Get-CanonicalPath -Path $RootPath -RequireExisting
        $executablePath = [string](Get-OptionalPropertyValue -Object $Process -Name 'ExecutablePath')
        if ([string]::IsNullOrWhiteSpace($executablePath)) { return $false }
        $canonicalExecutable = Get-CanonicalPath -Path $executablePath -RequireExisting
        $executableWithinRoot = Test-CanonicalPathWithinRoot -Path $canonicalExecutable -Root $root
        $line = [string](Get-OptionalPropertyValue -Object $Process -Name 'CommandLine')
        if (-not (Test-CommandLinePathToken -CommandLine $line -Path $root)) { return $false }
        if ($null -ne $ExpectedProcessIdentity) {
            $expectedPid = 0
            $actualPid = 0
            [void][int]::TryParse([string](Get-OptionalPropertyValue -Object $ExpectedProcessIdentity -Name 'process_id'), [ref]$expectedPid)
            [void][int]::TryParse([string](Get-OptionalPropertyValue -Object $Process -Name 'ProcessId'), [ref]$actualPid)
            if ($expectedPid -le 0 -or $actualPid -ne $expectedPid) { return $false }
            $expectedCreation = [string](Get-OptionalPropertyValue -Object $ExpectedProcessIdentity -Name 'creation_date')
            if (-not [string]::IsNullOrWhiteSpace($expectedCreation) -and $expectedCreation -ne [string](Get-OptionalPropertyValue -Object $Process -Name 'CreationDate')) { return $false }
            $expectedCommandHash = [string](Get-OptionalPropertyValue -Object $ExpectedProcessIdentity -Name 'command_line_sha256')
            if (-not [string]::IsNullOrWhiteSpace($expectedCommandHash) -and $expectedCommandHash -ne (Get-TextSha256 -Value $line)) { return $false }
            $expectedExecutable = [string](Get-OptionalPropertyValue -Object $ExpectedProcessIdentity -Name 'executable_path')
            if (-not [string]::IsNullOrWhiteSpace($expectedExecutable) -and (Get-CanonicalPath -Path $expectedExecutable -RequireExisting) -ne $canonicalExecutable) { return $false }
            $expectedStartIdentity = [string](Get-OptionalPropertyValue -Object $ExpectedProcessIdentity -Name 'start_identity')
            if ([string]::IsNullOrWhiteSpace($expectedStartIdentity) -or $expectedStartIdentity -cne (Get-ProcessGenerationIdentity -ProcessId $actualPid)) { return $false }
        }
        if ([string]::IsNullOrWhiteSpace($ExpectedPythonPath)) {
            if (-not $executableWithinRoot) { return $false }
        }
        else {
            $expectedPython = Get-CanonicalPath -Path $ExpectedPythonPath -RequireExisting
            if (-not (Test-CommandLinePathToken -CommandLine $line -Path $expectedPython)) { return $false }
            $expectedVenvPython = $null
            $expectedVenvPath = Join-Path $root '.venv\Scripts\python.exe'
            if (Test-Path -LiteralPath $expectedVenvPath -PathType Leaf) {
                try { $expectedVenvPython = Get-CanonicalPath -Path $expectedVenvPath -RequireExisting } catch { $expectedVenvPython = $null }
            }
            if ($null -ne $expectedVenvPython -and $expectedPython -eq $expectedVenvPython -and $canonicalExecutable -ne $expectedPython) {
                # Windows may report the base interpreter for a venv process.
                # Accept that relationship only when pyvenv.cfg binds the exact
                # candidate venv to the reported executable and version.
                $venv = Get-VenvInterpreterMetadata -RootPath $root -ExpectedPythonPath $expectedPython
                if ($null -eq $venv) { return $false }
                $baseExecutableMatched = $false
                foreach ($baseExecutable in @($venv.base_executables)) {
                    if ([string]$baseExecutable -ceq $canonicalExecutable) { $baseExecutableMatched = $true; break }
                }
                if (-not $baseExecutableMatched) {
                    try {
                        $reportedLeaf = [IO.Path]::GetFileName($canonicalExecutable)
                        $reportedParent = Get-CanonicalPath -Path ([IO.Path]::GetDirectoryName($canonicalExecutable)) -RequireExisting
                        $versionedBaseName = [string](Get-OptionalPropertyValue -Object $venv -Name 'versioned_base_name')
                        $baseExecutableMatched = (
                            -not [string]::IsNullOrWhiteSpace($versionedBaseName) -and
                            $reportedLeaf -ieq $versionedBaseName -and
                            $reportedParent -ceq [string]$venv.home
                        )
                    }
                    catch { $baseExecutableMatched = $false }
                }
                if (-not $baseExecutableMatched) { return $false }
                $expectedVersionKey = if (-not [string]::IsNullOrWhiteSpace($ExpectedInterpreterVersion)) {
                    Get-InterpreterVersionKey -Value $ExpectedInterpreterVersion
                }
                else { [string]$venv.version_key }
                $actualVersion = [string](Get-OptionalPropertyValue -Object $Process -Name 'ExecutableVersion')
                if ([string]::IsNullOrWhiteSpace($actualVersion)) { $actualVersion = Get-ExecutableVersion -Path $canonicalExecutable }
                $actualVersionKey = Get-InterpreterVersionKey -Value $actualVersion
                if ([string]::IsNullOrWhiteSpace($expectedVersionKey) -or [string]::IsNullOrWhiteSpace($actualVersionKey) -or $actualVersionKey -ne $expectedVersionKey) { return $false }
            }
            elseif ($canonicalExecutable -ne $expectedPython) {
                return $false
            }
            if (-not [string]::IsNullOrWhiteSpace($ExpectedInterpreterVersion)) {
                $actualVersion = [string](Get-OptionalPropertyValue -Object $Process -Name 'ExecutableVersion')
                if ([string]::IsNullOrWhiteSpace($actualVersion)) { $actualVersion = Get-ExecutableVersion -Path $canonicalExecutable }
                $actualVersionKey = Get-InterpreterVersionKey -Value $actualVersion
                $expectedVersionKey = Get-InterpreterVersionKey -Value $ExpectedInterpreterVersion
                if ([string]::IsNullOrWhiteSpace($expectedVersionKey) -or $actualVersionKey -ne $expectedVersionKey) { return $false }
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($ExpectedArguments)) {
            $argumentPattern = '(?i)(?<![A-Za-z0-9_.-])' + [regex]::Escape($ExpectedArguments.Trim()) + '(?![A-Za-z0-9_.-])'
            if (-not [regex]::IsMatch($line, $argumentPattern)) { return $false }
        }
        $processId = 0
        [void][int]::TryParse([string](Get-OptionalPropertyValue -Object $Process -Name 'ProcessId'), [ref]$processId)
        if ($null -ne $ExpectedListenerProcessId -and $processId -ne [int]$ExpectedListenerProcessId) { return $false }
        if ($null -ne $ExpectedListener) {
            $ownerValue = Get-OptionalPropertyValue -Object $ExpectedListener -Name 'OwningProcess'
            if ($null -eq $ownerValue) { $ownerValue = Get-OptionalPropertyValue -Object $ExpectedListener -Name 'owning_process' }
            $owner = 0
            [void][int]::TryParse([string]$ownerValue, [ref]$owner)
            $address = [string](Get-OptionalPropertyValue -Object $ExpectedListener -Name 'LocalAddress')
            if ([string]::IsNullOrWhiteSpace($address)) { $address = [string](Get-OptionalPropertyValue -Object $ExpectedListener -Name 'local_address') }
            $portValue = Get-OptionalPropertyValue -Object $ExpectedListener -Name 'LocalPort'
            if ($null -eq $portValue) { $portValue = Get-OptionalPropertyValue -Object $ExpectedListener -Name 'local_port' }
            $port = 0
            [void][int]::TryParse([string]$portValue, [ref]$port)
            $state = [string](Get-OptionalPropertyValue -Object $ExpectedListener -Name 'State')
            if ([string]::IsNullOrWhiteSpace($state)) { $state = [string](Get-OptionalPropertyValue -Object $ExpectedListener -Name 'state') }
            if ($owner -ne $processId -or $address -ne '127.0.0.1' -or $state -ine 'Listen') { return $false }
            if ($null -ne $ExpectedListenerPort -and $port -ne [int]$ExpectedListenerPort) { return $false }
        }
        elseif ($null -ne $ExpectedListenerPort) {
            $processPort = Get-OptionalPropertyValue -Object $Process -Name 'ListenerPort'
            if ($null -eq $processPort) { $processPort = Get-OptionalPropertyValue -Object $Process -Name 'LocalPort' }
            if ($null -eq $processPort -or [int]$processPort -ne [int]$ExpectedListenerPort) { return $false }
        }
        if ($null -ne $ExpectedTaskIdentity -and -not (Test-CanonicalTaskActionBinding -Identity $ExpectedTaskIdentity -ExpectedRoot $root -ExpectedPythonPath $ExpectedPythonPath -ExpectedArguments $ExpectedArguments -ExpectedPrincipal $ExpectedPrincipal -ExpectedApiPort $ExpectedApiPort)) { return $false }
        $moduleMatched = $false
        foreach ($moduleName in @($ModuleNames)) {
            if (Test-CommandLineModuleToken -CommandLine $line -ModuleName $moduleName) { $moduleMatched = $true; break }
        }
        return $moduleMatched
    }
    catch {
        return $false
    }
}

function Get-InstallProcessSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [string]$ExpectedPythonPath
    )

    Assert-NoReparsePath -Path $RootPath | Out-Null
    $canonicalRoot = Get-CanonicalPath -Path $RootPath
    $expectedPython = if ([string]::IsNullOrWhiteSpace($ExpectedPythonPath)) { Join-Path $canonicalRoot '.venv\Scripts\python.exe' } else { $ExpectedPythonPath }
    $rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $matches = @()
    foreach ($row in $rows) {
        $name = ([string]$row.Name).ToLowerInvariant()
        if (-not [regex]::IsMatch($name, '^python(?:w|[0-9]+(?:\.[0-9]+)?)?\.exe$')) { continue }
        $commandLine = [string]$row.CommandLine
        if (-not (Test-CanonicalProcessIdentity -Process $row -RootPath $canonicalRoot -ExpectedPythonPath $expectedPython)) { continue }
        $isApi = Test-CommandLineModuleToken -CommandLine $commandLine -ModuleName 'app.api_server'
        $isWorker = Test-CommandLineModuleToken -CommandLine $commandLine -ModuleName 'app.worker'
        $matches += [pscustomobject]@{
            process_id = [int]$row.ProcessId
            parent_process_id = [int]$row.ParentProcessId
            name = [string]$row.Name
            executable_path = [string]$row.ExecutablePath
            module = if ($isApi) { 'app.api_server' } else { 'app.worker' }
            identity_root = $canonicalRoot
            command_line = Limit-Text -Value $commandLine -MaxChars $script:MaxProcessCommandLineChars
            command_line_sha256 = Get-TextSha256 -Value $commandLine
            creation_date = [string]$row.CreationDate
            start_identity = Get-ProcessGenerationIdentity -ProcessId ([int]$row.ProcessId)
        }
    }
    return @($matches | Sort-Object process_id)
}

function Get-ProcessGenerationIdentity {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    $handle = [IntPtr]::Zero
    try {
        $handle = [HwpxInstallNative.ProcessAuthority]::OpenForQuery($ProcessId)
        return ('win-filetime:' + ([HwpxInstallNative.ProcessAuthority]::GetStartIdentity($handle)).ToLowerInvariant())
    }
    finally {
        if ($handle -ne [IntPtr]::Zero) { [HwpxInstallNative.ProcessAuthority]::Close($handle) }
    }
}

function Test-InstallProcessIdentityMatch {
    param(
        [Parameter(Mandatory = $true)][object]$Expected,
        [Parameter(Mandatory = $true)][object]$Actual
    )
    foreach ($field in @('process_id', 'creation_date', 'executable_path', 'command_line_sha256', 'module', 'identity_root', 'start_identity')) {
        $expectedValue = [string](Get-OptionalPropertyValue -Object $Expected -Name $field)
        $actualValue = [string](Get-OptionalPropertyValue -Object $Actual -Name $field)
        if ([string]::IsNullOrWhiteSpace($expectedValue) -or $expectedValue -cne $actualValue) { return $false }
    }
    return $true
}

function Stop-InstallProcesses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [int[]]$PreserveProcessIds = @(),
        [AllowNull()][object[]]$PreserveProcessIdentities = @(),
        [string]$ExpectedPythonPath,
        [AllowNull()][object[]]$InitialProcessSnapshot = @(),
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 15
    )

    $preserved = @{}
    foreach ($processId in @($PreserveProcessIds)) {
        if ($null -ne $processId -and [int]$processId -gt 0) {
            $preserved[[int]$processId] = $null
        }
    }
    foreach ($identity in @($PreserveProcessIdentities)) {
        if ($null -eq $identity) { throw 'PreserveProcessIdentities contains a null row.' }
        $identityPid = 0
        [void][int]::TryParse([string](Get-OptionalPropertyValue -Object $identity -Name 'process_id'), [ref]$identityPid)
        if ($identityPid -le 0) { throw 'PreserveProcessIdentities contains an invalid process id.' }
        if ([string]::IsNullOrWhiteSpace([string](Get-OptionalPropertyValue -Object $identity -Name 'creation_date')) -or
            [string]::IsNullOrWhiteSpace([string](Get-OptionalPropertyValue -Object $identity -Name 'command_line_sha256')) -or
            [string]::IsNullOrWhiteSpace([string](Get-OptionalPropertyValue -Object $identity -Name 'start_identity'))) {
            throw "PreserveProcessIdentities must contain complete sealed identity evidence: $identityPid"
        }
        $preserved[$identityPid] = $identity
    }
    Assert-NoReparsePath -Path $RootPath | Out-Null
    $canonicalRoot = Get-CanonicalPath -Path $RootPath
    $expectedPython = if ([string]::IsNullOrWhiteSpace($ExpectedPythonPath)) { Join-Path $canonicalRoot '.venv\Scripts\python.exe' } else { $ExpectedPythonPath }
    $observed = @($InitialProcessSnapshot)
    if ($observed.Count -gt 0) {
        # A caller may have observed candidate processes before a rollback
        # coordination gap. Preserve those rows so a process that exits before
        # this function's fresh scan is still accounted for as released.
        $observedPids = @{}
        foreach ($row in $observed) {
            if ($null -eq $row) { throw 'Initial process snapshot contains a null row.' }
            $observedPid = 0
            [void][int]::TryParse([string](Get-OptionalPropertyValue -Object $row -Name 'process_id'), [ref]$observedPid)
            if ($observedPid -le 0) { throw 'Initial process snapshot contains an invalid process id.' }
            if ($observedPids.ContainsKey($observedPid)) { throw "Initial process snapshot contains duplicate process id: $observedPid" }
            $observedRoot = [string](Get-OptionalPropertyValue -Object $row -Name 'identity_root')
            if ($observedRoot -cne $canonicalRoot) { throw "Initial process snapshot is bound to another root: $observedPid" }
            $observedCommandLine = [string](Get-OptionalPropertyValue -Object $row -Name 'command_line')
            $observedCommandHash = [string](Get-OptionalPropertyValue -Object $row -Name 'command_line_sha256')
            if ([string]::IsNullOrWhiteSpace($observedCommandLine) -or
                $observedCommandHash -cne (Get-TextSha256 -Value $observedCommandLine)) {
                throw "Initial process snapshot has invalid command-line evidence: $observedPid"
            }
            if ([string]::IsNullOrWhiteSpace([string](Get-OptionalPropertyValue -Object $row -Name 'start_identity'))) {
                throw "Initial process snapshot has no sealed process-generation identity: $observedPid"
            }
            $observedModule = [string](Get-OptionalPropertyValue -Object $row -Name 'module')
            if ($observedModule -notin @('app.api_server', 'app.worker')) {
                throw "Initial process snapshot has an unsupported module: $observedPid"
            }
            $observedPids[$observedPid] = $true
        }
        $fresh = @(Get-InstallProcessSnapshot -RootPath $RootPath -ExpectedPythonPath $expectedPython)
        $merged = New-Object System.Collections.ArrayList
        foreach ($row in $observed) { [void]$merged.Add($row) }
        foreach ($row in $fresh) {
            $freshPid = [int]$row.process_id
            if (-not $observedPids.ContainsKey($freshPid)) { [void]$merged.Add($row) }
        }
        $before = @($merged)
    }
    else {
        $before = @(Get-InstallProcessSnapshot -RootPath $RootPath -ExpectedPythonPath $expectedPython)
    }
    $stopped = New-Object System.Collections.ArrayList
    $raceReleased = New-Object System.Collections.ArrayList
    foreach ($row in @($before | Sort-Object process_id -Descending)) {
        $processId = [int]$row.process_id
        if ($preserved.ContainsKey($processId)) {
            $preservedIdentity = $preserved[$processId]
            if ($null -eq $preservedIdentity -or -not (Test-InstallProcessIdentityMatch -Expected $preservedIdentity -Actual $row)) {
                # A reusable PID is not a preserved process. It remains an
                # owned candidate only when the complete sealed identity still
                # matches the current snapshot.
            }
            else { continue }
        }
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            [void]$raceReleased.Add($processId)
            continue
        }
        $processHandle = [IntPtr]::Zero
        try {
            # Revalidate the canonical root and the exact process object at
            # the mutation boundary.  PID/path snapshots alone are not safe
            # against PID reuse or a junction swap.
            Assert-NoReparsePath -Path $canonicalRoot | Out-Null
            $currentCim = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
            $currentIdentity = if ($currentCim) {
                Test-CanonicalProcessIdentity -Process $currentCim -RootPath $canonicalRoot -ExpectedPythonPath $expectedPython -ModuleNames @([string]$row.module) -ExpectedProcessIdentity $row
            }
            else { $false }
            if (-not $currentIdentity) {
                [void]$raceReleased.Add($processId)
                continue
            }
            # Open a kernel handle only after the CIM/root/command-line
            # identity has been revalidated. The handle pins this process
            # generation; a later PID reuse cannot redirect TerminateProcess.
            $processHandle = [HwpxInstallNative.ProcessAuthority]::Open($processId)
            $nativeStartIdentity = [HwpxInstallNative.ProcessAuthority]::GetStartIdentity($processHandle)
            $nativeStartIdentity = 'win-filetime:' + $nativeStartIdentity.ToLowerInvariant()
            $expectedStartIdentity = [string](Get-OptionalPropertyValue -Object $row -Name 'start_identity')
            if ([string]::IsNullOrWhiteSpace($expectedStartIdentity) -or $nativeStartIdentity -cne $expectedStartIdentity) {
                throw "Candidate process generation changed at the native handle boundary: $processId"
            }
            $expectedCreation = [string](Get-OptionalPropertyValue -Object $row -Name 'creation_date')
            $currentCreation = [string](Get-OptionalPropertyValue -Object $currentCim -Name 'CreationDate')
            if (-not [string]::IsNullOrWhiteSpace($expectedCreation) -and $expectedCreation -ne $currentCreation) {
                throw "Candidate process generation changed at the handle boundary: $processId"
            }
            if (-not [HwpxInstallNative.ProcessAuthority]::IsAlive($processHandle)) {
                [void]$raceReleased.Add($processId)
                continue
            }
            if (-not (Test-CanonicalProcessIdentity -Process $currentCim -RootPath $canonicalRoot -ExpectedPythonPath $expectedPython -ModuleNames @([string]$row.module) -ExpectedProcessIdentity $row)) {
                throw "Candidate process identity changed at the stop boundary: $processId"
            }
            [HwpxInstallNative.ProcessAuthority]::Terminate($processHandle)
            $process.WaitForExit(5000) | Out-Null
            [void]$stopped.Add([pscustomobject]@{
                process_id = $processId
                name = [string]$row.name
                start_identity = $nativeStartIdentity
                termination_authority = 'verified-native-process-handle'
            })
        }
        catch {
            # A process may exit after the snapshot/Get-Process lookup but
            # before Stop-Process reaches it. Re-check ownership; only a still
            # present candidate process remains a rollback failure.
            $stillOwned = @(
                Get-InstallProcessSnapshot -RootPath $RootPath -ExpectedPythonPath $expectedPython |
                    Where-Object { [int]$_.process_id -eq $processId }
            )
            if ($stillOwned.Count -eq 0) {
                [void]$raceReleased.Add($processId)
                continue
            }
            throw "Failed to stop candidate process $processId before rollback: $($_.Exception.Message)"
        }
        finally {
            if ($processHandle -ne [IntPtr]::Zero) {
                try { [HwpxInstallNative.ProcessAuthority]::Close($processHandle) } catch { }
            }
            if ($null -ne $process) {
                try { $process.Dispose() } catch { }
            }
        }
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $remaining = @()
    do {
        Assert-NoReparsePath -Path $canonicalRoot | Out-Null
        $remaining = @()
        foreach ($row in @(Get-InstallProcessSnapshot -RootPath $RootPath -ExpectedPythonPath $expectedPython)) {
            $processId = [int]$row.process_id
            $preservedIdentity = if ($preserved.ContainsKey($processId)) { $preserved[$processId] } else { $null }
            if (-not $preservedIdentity -or -not (Test-InstallProcessIdentityMatch -Expected $preservedIdentity -Actual $row)) {
                $remaining += $processId
            }
        }
        if ($remaining.Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    if ($remaining.Count -gt 0) {
        throw "Candidate process handles remain after rollback stop: $($remaining -join ',')"
    }
    return [pscustomobject]@{
        ok = $true
        RootPath = $canonicalRoot
        root = $canonicalRoot
        snapshot_count = $before.Count
        stopped = @($stopped)
        race_released_process_ids = @($raceReleased | Sort-Object -Unique)
        preserved_process_ids = @($preserved.Keys | Sort-Object)
        remaining = @($remaining)
    }
}

function Wait-ScheduledTaskInactive {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [string]$TaskPath = $script:DefaultTaskPath,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 15
    )
    $safePath = Assert-CanonicalScheduledTaskPath -TaskPath $TaskPath
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $task = Get-ScheduledTaskExact -TaskName $TaskName -TaskPath $safePath -AllowMissing
        if ($null -eq $task) { return $true }
        $state = [string]$task.State
        if ($state -notin @('Running', 'Queued')) { return $true }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Test-ScheduledTaskIdentityExact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Actual,
        [Parameter(Mandatory = $true)][object]$Expected
    )
    if (-not [bool](Get-OptionalPropertyValue -Object $Actual -Name 'exists') -or
        -not [bool](Get-OptionalPropertyValue -Object $Expected -Name 'exists')) { return $false }
    return (
        [string](Get-OptionalPropertyValue -Object $Actual -Name 'task_name') -ceq [string](Get-OptionalPropertyValue -Object $Expected -Name 'task_name') -and
        [string](Get-OptionalPropertyValue -Object $Actual -Name 'task_path') -ceq [string](Get-OptionalPropertyValue -Object $Expected -Name 'task_path') -and
        [string](Get-OptionalPropertyValue -Object $Actual -Name 'task_identity_hash') -ceq [string](Get-OptionalPropertyValue -Object $Expected -Name 'task_identity_hash') -and
        [string](Get-OptionalPropertyValue -Object $Actual -Name 'xml') -cne $null -and
        [string](Get-OptionalPropertyValue -Object $Actual -Name 'xml') -ceq [string](Get-OptionalPropertyValue -Object $Expected -Name 'xml')
    )
}

function Stop-ScheduledTaskExactAndWait {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [string]$TaskPath = $script:DefaultTaskPath,
        [object]$ExpectedIdentity
    )
    $current = Get-ScheduledTaskIdentity -TaskName $TaskName -TaskPath $TaskPath
    if (-not [bool]$current.exists) {
        if ($null -ne $ExpectedIdentity) { throw "Scheduled task disappeared before quiescence: $TaskName" }
        return $current
    }
    if ($null -ne $ExpectedIdentity -and -not (Test-ScheduledTaskIdentityExact -Actual $current -Expected $ExpectedIdentity)) {
        throw "Scheduled task identity changed before quiescence: $TaskName"
    }
    $task = Get-ScheduledTaskExact -TaskName $TaskName -TaskPath $TaskPath
    if ([string]$task.State -in @('Running', 'Queued')) {
        Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
        if (-not (Wait-ScheduledTaskInactive -TaskName $TaskName -TaskPath $TaskPath)) {
            throw "Scheduled task remained active after stop request: $TaskName"
        }
    }
    $after = Get-ScheduledTaskIdentity -TaskName $TaskName -TaskPath $TaskPath
    if (-not [bool]$after.exists) { throw "Scheduled task disappeared during quiescence: $TaskName" }
    if ($null -ne $ExpectedIdentity -and -not (Test-ScheduledTaskIdentityExact -Actual $after -Expected $ExpectedIdentity)) {
        throw "Scheduled task identity changed during quiescence: $TaskName"
    }
    return $after
}

function Register-ScheduledTaskExactNoClobber {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [string]$TaskPath = $script:DefaultTaskPath,
        [Parameter(Mandatory = $true)][string]$Xml,
        [object]$ExpectedCurrentIdentity,
        [switch]$SkipQuiescence
    )
    if ([string]::IsNullOrWhiteSpace($Xml)) { throw "Scheduled task XML is missing: $TaskName" }
    $current = Get-ScheduledTaskIdentity -TaskName $TaskName -TaskPath $TaskPath
    if ([bool]$current.exists) {
        if ($null -eq $ExpectedCurrentIdentity -or -not (Test-ScheduledTaskIdentityExact -Actual $current -Expected $ExpectedCurrentIdentity)) {
            throw "Refusing to overwrite a raced scheduled task definition: $TaskName"
        }
        if (-not $SkipQuiescence -or [string]$current.state -eq 'Queued') {
            Stop-ScheduledTaskExactAndWait -TaskName $TaskName -TaskPath $TaskPath -ExpectedIdentity $ExpectedCurrentIdentity | Out-Null
        }
        $beforeUnregister = Get-ScheduledTaskIdentity -TaskName $TaskName -TaskPath $TaskPath
        if (-not (Test-ScheduledTaskIdentityExact -Actual $beforeUnregister -Expected $ExpectedCurrentIdentity)) {
            throw "Scheduled task identity changed before exact unregister: $TaskName"
        }
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false -ErrorAction Stop
        if (Get-ScheduledTaskExact -TaskName $TaskName -TaskPath $TaskPath -AllowMissing) {
            throw "Scheduled task remained after exact unregister: $TaskName"
        }
    }
    else {
        if ($null -ne $ExpectedCurrentIdentity) { throw "Scheduled task disappeared before exact restore: $TaskName" }
    }
    # No -Force: a concurrent registration is a collision, not permission to
    # clobber another client's definition.
    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Xml $Xml -ErrorAction Stop | Out-Null
    $restored = Get-ScheduledTaskIdentity -TaskName $TaskName -TaskPath $TaskPath
    if (-not [bool]$restored.exists -or [string]$restored.xml -cne $Xml) {
        throw "Scheduled task XML readback did not match the exact restore: $TaskName"
    }
    return $restored
}

function Wait-ScheduledTaskRunning {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [string]$TaskPath = $script:DefaultTaskPath,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 30
    )
    $safePath = Assert-CanonicalScheduledTaskPath -TaskPath $TaskPath
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $task = Get-ScheduledTaskExact -TaskName $TaskName -TaskPath $safePath -AllowMissing
        if ($null -eq $task) { return $false }
        if ([string]$task.State -eq 'Running') { return $true }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Get-ReceiptPropertyValue {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    return Get-OptionalPropertyValue -Object $Object -Name $Name
}

function ConvertTo-BoundedReceiptValue {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Value,
        [string]$PropertyName = '',
        [int]$Depth = 0
    )
    if ($null -eq $Value) { return $null }
    if ($Depth -ge 8) { return '[receipt depth omitted]' }
    $propertyKey = if ($null -eq $PropertyName) { '' } else { $PropertyName.ToLowerInvariant() }
    if ($propertyKey -in @('diagnostic', 'diagnostics', 'raw_diagnostic', 'native_diagnostic', 'failure_diagnostic')) {
        $fieldNames = @()
        $propertyCount = 0
        if ($Value -is [System.Collections.IDictionary]) {
            $propertyCount = @($Value.Keys).Count
            $fieldNames = @($Value.Keys | ForEach-Object { [string]$_ } | Select-Object -First 16)
        }
        elseif ($Value -isnot [string] -and $Value.PSObject) {
            $propertyCount = @($Value.PSObject.Properties).Count
            $fieldNames = @($Value.PSObject.Properties.Name | Select-Object -First 16)
        }
        return [ordered]@{
            omitted = $true
            reason = 'diagnostic-value-bounded'
            property_count = [int]$propertyCount
            fields = @($fieldNames)
        }
    }
    if ($Value -is [string] -or $Value -is [char]) {
        return Limit-Text -Value ([string]$Value) -MaxChars 8192
    }
    if ($Value -is [bool] -or $Value -is [byte] -or $Value -is [int16] -or $Value -is [int32] -or $Value -is [int64] -or $Value -is [uint16] -or $Value -is [uint32] -or $Value -is [uint64] -or $Value -is [single] -or $Value -is [double] -or $Value -is [decimal]) {
        return $Value
    }
    if ($Value -is [datetime] -or $Value -is [guid]) { return [string]$Value }
    if ($Value -is [byte[]]) {
        return [ordered]@{ omitted = $true; reason = 'binary-value-bounded'; byte_count = [int64]$Value.Length }
    }
    if ($Value -is [System.Collections.IDictionary]) {
        if ($propertyKey -eq 'files') {
            return [ordered]@{ omitted = $true; reason = 'file-list-bounded'; item_count = [int]@($Value.Keys).Count }
        }
        $output = [ordered]@{}
        foreach ($key in @($Value.Keys)) {
            $name = [string]$key
            $output[$name] = ConvertTo-BoundedReceiptValue -Value $Value[$key] -PropertyName $name -Depth ($Depth + 1)
        }
        return $output
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $items = @($Value)
        if ($propertyKey -eq 'files') {
            return [ordered]@{ omitted = $true; reason = 'file-list-bounded'; item_count = [int]$items.Count }
        }
        if ($items.Count -le 64) {
            $boundedItems = @()
            foreach ($item in $items) { $boundedItems += ConvertTo-BoundedReceiptValue -Value $item -PropertyName $PropertyName -Depth ($Depth + 1) }
            return ,$boundedItems
        }
        $boundedItems = @()
        foreach ($item in @($items | Select-Object -First 32)) { $boundedItems += ConvertTo-BoundedReceiptValue -Value $item -PropertyName $PropertyName -Depth ($Depth + 1) }
        $boundedItems += [ordered]@{ omitted = $true; reason = 'array-length-bounded'; omitted_count = [int]($items.Count - 64) }
        foreach ($item in @($items | Select-Object -Last 32)) { $boundedItems += ConvertTo-BoundedReceiptValue -Value $item -PropertyName $PropertyName -Depth ($Depth + 1) }
        return ,$boundedItems
    }
    if ($Value.PSObject) {
        $output = [ordered]@{}
        foreach ($property in @($Value.PSObject.Properties)) {
            $name = [string]$property.Name
            $output[$name] = ConvertTo-BoundedReceiptValue -Value $property.Value -PropertyName $name -Depth ($Depth + 1)
        }
        return $output
    }
    return Limit-Text -Value ([string]$Value) -MaxChars 8192
}

function New-BoundedReceiptProjection {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [int64]$OriginalBytes
    )
    $projection = [ordered]@{}
    foreach ($name in @(
        'schema_version', 'status', 'failure_class', 'status_code', 'phase', 'source_root',
        'install_root', 'candidate_root', 'candidate_generation', 'snapshot_path', 'snapshot_identity', 'snapshot_cleanup', 'backup_identity', 'api_base_url', 'api_port',
        'source_identity', 'candidate_identity', 'failed_predicates', 'failure_predicates',
        'failed_gates', 'failure', 'failure_detail', 'failure_reason', 'errors', 'cleanup',
        'rollback', 'processes_released', 'tasks_removed', 'listener', 'checks',
        'dependency_completeness'
    )) {
        $propertyValue = Get-ReceiptPropertyValue -Object $Value -Name $name
        if ($null -ne $propertyValue) {
            $projection[$name] = ConvertTo-BoundedReceiptValue -Value $propertyValue -PropertyName $name
        }
    }
    $projection['receipt_bounds'] = [ordered]@{
        bounded = $true
        original_bytes = [int64]$OriginalBytes
        max_bytes = [int64]$script:MaxReceiptBytes
        diagnostic_values_omitted = $true
    }
    return $projection
}

function New-MinimalReceiptProjection {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [int64]$OriginalBytes
    )
    $projection = [ordered]@{}
    foreach ($name in @(
        'schema_version', 'status', 'failure_class', 'status_code', 'phase', 'source_root',
        'install_root', 'candidate_root', 'candidate_generation', 'snapshot_path', 'snapshot_identity', 'snapshot_cleanup', 'backup_identity', 'api_base_url', 'api_port',
        'source_identity', 'candidate_identity', 'failed_predicates', 'failure_predicates',
        'failed_gates', 'failure', 'failure_detail', 'failure_reason', 'errors', 'cleanup',
        'rollback', 'processes_released', 'tasks_removed'
    )) {
        $propertyValue = Get-ReceiptPropertyValue -Object $Value -Name $name
        if ($null -ne $propertyValue) {
            $projection[$name] = ConvertTo-BoundedReceiptValue -Value $propertyValue -PropertyName $name
        }
    }
    $projection['receipt_bounds'] = [ordered]@{
        bounded = $true
        minimal = $true
        original_bytes = [int64]$OriginalBytes
        max_bytes = [int64]$script:MaxReceiptBytes
        diagnostic_values_omitted = $true
    }
    return $projection
}

function Write-JsonReceipt {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value,
        [switch]$NoProjection
    )
    $target = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path))
    $parent = Split-Path -Parent $target
    $receiptFaultMode = [string]([Environment]::GetEnvironmentVariable('HWPX_TEST_RECEIPT_FAULT'))
    if ($receiptFaultMode -eq 'serialization') {
        throw 'injected receipt serialization failure'
    }
    if ($parent) {
        Assert-NoReparsePath -Path $parent | Out-Null
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        Assert-NoReparsePath -Path $parent | Out-Null
    }
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        Assert-NoReparsePath -Path $target | Out-Null
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $originalBytes = 0L
    $originalJson = '{}'
    $serializationFailed = $false
    try {
        $originalJson = [string]($Value | ConvertTo-Json -Depth 30)
        $originalBytes = $encoding.GetByteCount($originalJson + [Environment]::NewLine)
    }
    catch {
        $serializationFailed = $true
        $originalBytes = [int64]$script:MaxReceiptBytes + 1
    }
    $json = $originalJson
    $payload = $json + [Environment]::NewLine
    if ($NoProjection -and $serializationFailed) {
        throw "Transactional JSON payload could not be serialized without projection: $Path"
    }
    if ($NoProjection -and $encoding.GetByteCount($payload) -gt $script:MaxReceiptBytes) {
        throw "Transactional JSON payload exceeds the bounded size limit of $script:MaxReceiptBytes bytes: $Path"
    }
    if (-not $NoProjection -and ($serializationFailed -or $encoding.GetByteCount($payload) -gt $script:MaxReceiptBytes)) {
        $projected = New-BoundedReceiptProjection -Value $Value -OriginalBytes $originalBytes
        $json = [string]($projected | ConvertTo-Json -Depth 30)
        $payload = $json + [Environment]::NewLine
    }
    if (-not $NoProjection -and $encoding.GetByteCount($payload) -gt $script:MaxReceiptBytes) {
        $minimal = New-MinimalReceiptProjection -Value $Value -OriginalBytes $originalBytes
        $json = [string]($minimal | ConvertTo-Json -Depth 30)
        $payload = $json + [Environment]::NewLine
    }
    if ($encoding.GetByteCount($payload) -gt $script:MaxReceiptBytes) {
        throw "JSON receipt exceeds the bounded size limit of $script:MaxReceiptBytes bytes after projection: $Path"
    }
    $receiptLock = Enter-ReceiptPathLock -ReceiptPath $target
    $temporaryPath = Join-Path $parent ('.receipt-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backupPath = Join-Path $parent ('.receipt-backup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $targetExistedBefore = Test-Path -LiteralPath $target -PathType Leaf
    $temporaryStream = $null
    $replacementCommitted = $false
    $readbackValidated = $false
    $targetIdentityAfterCommit = $null
    $backupHoldPath = $null
    try {
        [System.IO.File]::WriteAllText($temporaryPath, $payload, $encoding)
        # Flush the complete temporary receipt before the atomic replacement.
        $temporaryStream = [System.IO.File]::Open($temporaryPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::Read)
        $temporaryStream.Flush($true)
        $temporaryStream.Dispose()
        $temporaryStream = $null
        if ($receiptFaultMode -eq 'write') {
            throw 'injected receipt write failure'
        }
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            [System.IO.File]::Replace($temporaryPath, $target, $backupPath)
            $replacementCommitted = $true
        }
        else {
            [System.IO.File]::Move($temporaryPath, $target)
            $replacementCommitted = $true
        }
        $targetIdentityAfterCommit = Get-PathObjectIdentity -Path $target -RequireExisting
        if ($receiptFaultMode -eq 'readback') {
            throw 'injected receipt readback failure'
        }
        $readbackPath = Get-CanonicalPath -Path $target -RequireExisting
        $readbackPayload = [System.IO.File]::ReadAllText($readbackPath, $encoding)
        if ($readbackPayload -cne $payload) {
            throw "JSON receipt readback did not match the atomically committed bytes: $Path"
        }
        try {
            ConvertFrom-Json -InputObject $readbackPayload | Out-Null
        }
        catch {
            throw "JSON receipt readback was not valid JSON: $Path"
        }
        $readbackValidated = $true
        return $readbackPath
    }
    finally {
        if ($null -ne $temporaryStream) { $temporaryStream.Dispose() }
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            if ($readbackValidated) {
                Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
            }
            else {
                # Never discard the last known-good receipt when replacement
                # or readback validation fails. Restore it to the target when
                # possible and retain any uncertain new bytes as HOLD evidence.
                $backupHoldPath = $backupPath + '.HOLD'
                try {
                    if (Test-Path -LiteralPath $target -PathType Leaf) {
                        [System.IO.File]::Replace($backupPath, $target, $backupHoldPath)
                    }
                    else {
                        [System.IO.File]::Move($backupPath, $target)
                    }
                }
                catch {
                    # Retain the backup at its stable path if restoration is
                    # not possible; deleting it would make recovery unsafe.
                    $backupHoldPath = $backupPath
                }
            }
        }
        elseif ($replacementCommitted -and -not $readbackValidated -and -not $targetExistedBefore -and (Test-Path -LiteralPath $target -PathType Leaf)) {
            # There was no previous receipt to restore.  Remove the uncertain
            # new target from the namespace but retain its bytes as HOLD
            # evidence rather than silently presenting a malformed receipt.
            $backupHoldPath = $target + '.HOLD'
            try {
                Assert-PathObjectIdentity -Path $target -ExpectedIdentity $targetIdentityAfterCommit | Out-Null
                [System.IO.File]::Move($target, $backupHoldPath)
            }
            catch {
                # Keep the uncertain target if it cannot be moved safely; the
                # failure remains visible to the caller and is never reported
                # as a committed receipt.
            }
        }
        Exit-PathMutex -Lock $receiptLock
    }
}

function Save-InstallSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SnapshotPath,
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [string[]]$TaskName = @(),
        [string]$TaskPath = $script:DefaultTaskPath,
        [Parameter(Mandatory = $true)][string]$OwnerRunId,
        [hashtable]$TaskStateOverride = @{},
        [hashtable]$TaskIdentityOverride = @{}
    )
    $tasks = @($TaskName | ForEach-Object {
        if ($TaskIdentityOverride.ContainsKey($_)) {
            $TaskIdentityOverride[$_]
        }
        else {
            Get-ScheduledTaskIdentity -TaskName $_ -TaskPath $TaskPath
        }
    })
    if ($tasks.Count -gt $script:MaxSnapshotTasks) { throw "Install snapshot contains too many scheduled tasks: $($tasks.Count)" }
    foreach ($task in $tasks) {
        if ([bool](Get-OptionalPropertyValue -Object $task -Name 'exists')) {
            if ([string]::IsNullOrWhiteSpace([string](Get-OptionalPropertyValue -Object $task -Name 'xml')) -or
                [string]::IsNullOrWhiteSpace([string](Get-OptionalPropertyValue -Object $task -Name 'task_identity_hash')) -or
                $null -eq (Get-OptionalPropertyValue -Object $task -Name 'settings') -or
                [int](Get-OptionalPropertyValue -Object $task -Name 'action_count') -lt 1 -or
                [int](Get-OptionalPropertyValue -Object $task -Name 'xml_action_count') -lt 1) {
                throw "Existing scheduled task snapshot is incomplete: $($task.task_name)"
            }
        }
        $taskNameValue = [string](Get-OptionalPropertyValue -Object $task -Name 'task_name')
        if ($TaskStateOverride.ContainsKey($taskNameValue)) {
            $task.state = [string]$TaskStateOverride[$taskNameValue]
        }
    }
    $processes = @(Get-InstallProcessSnapshot -RootPath $InstallRoot)
    if ($processes.Count -gt $script:MaxSnapshotProcesses) { throw "Install snapshot contains too many candidate processes: $($processes.Count)" }
    $snapshot = [ordered]@{
        schema_version = 'hwpx/windows-install-snapshot/v1'
        snapshot_schema = 'hwpx/windows-install-snapshot/v1'
        captured_at_utc = [DateTime]::UtcNow.ToString('o')
        install_root = Get-CanonicalPath -Path $InstallRoot
        owner_run_id = $OwnerRunId
        tasks = $tasks
        processes = $processes
    }
    $writtenPath = Write-JsonReceipt -Path $SnapshotPath -Value $snapshot -NoProjection
    $readback = [IO.File]::ReadAllText((Get-CanonicalPath -Path $writtenPath -RequireExisting)) | ConvertFrom-Json
    if ([string]$readback.schema_version -ne 'hwpx/windows-install-snapshot/v1' -or
        [string]$readback.snapshot_schema -ne 'hwpx/windows-install-snapshot/v1' -or
        -not ($readback.PSObject.Properties.Name -contains 'tasks') -or
        -not ($readback.PSObject.Properties.Name -contains 'processes')) {
        throw "Install snapshot readback did not preserve the transactional schema: $SnapshotPath"
    }
    if (@($readback.tasks).Count -ne $tasks.Count -or @($readback.processes).Count -ne $processes.Count) {
        throw "Install snapshot readback changed task/process cardinality: $SnapshotPath"
    }
    return $readback
}

function Read-VerifiedInstallSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SnapshotPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSnapshotSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedSnapshotIdentity,
        [Parameter(Mandatory = $true)][string]$ExpectedRunId
    )
    $canonical = Get-CanonicalPath -Path $SnapshotPath -RequireExisting
    $identityBefore = Assert-PathObjectIdentity -Path $canonical -ExpectedIdentity $ExpectedSnapshotIdentity
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $canonical,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        if ($stream.Length -gt $script:MaxSnapshotBytes) {
            throw "Install snapshot exceeds the bounded size limit of $script:MaxSnapshotBytes bytes: $canonical"
        }
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $actualHash = ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $sha.Dispose()
        }
        if ([string]$actualHash -cne ([string]$ExpectedSnapshotSha256).ToLowerInvariant()) {
            throw "Install snapshot SHA-256 did not match the sealed preimage: $canonical"
        }
        $stream.Position = 0
        $bytes = New-Object byte[] ([int]$stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) { throw "Install snapshot ended before the sealed byte count was read: $canonical" }
            $offset += $read
        }
        $identityAfter = Assert-PathObjectIdentity -Path $canonical -ExpectedIdentity $identityBefore
        $snapshotText = [System.Text.Encoding]::UTF8.GetString($bytes)
        # Windows PowerShell 5.1 Set-Content -Encoding UTF8 writes a BOM;
        # normalize only that transport marker after the byte/hash/identity
        # checks, never before them.
        if ($snapshotText.Length -gt 0 -and [int][char]$snapshotText[0] -eq 0xFEFF) {
            $snapshotText = $snapshotText.Substring(1)
        }
        # Equivalent direct PowerShell 5.1 UTF-8 parse for BOM-free payloads:
        # $snapshot = [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
        $snapshot = $snapshotText | ConvertFrom-Json
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    if ([string]$snapshot.schema_version -ne 'hwpx/windows-install-snapshot/v1' -or
        [string]$snapshot.snapshot_schema -ne 'hwpx/windows-install-snapshot/v1' -or
        -not ($snapshot.PSObject.Properties.Name -contains 'tasks') -or
        -not ($snapshot.PSObject.Properties.Name -contains 'processes') -or
        [string]::IsNullOrWhiteSpace([string]$snapshot.owner_run_id)) {
        throw "Verified install snapshot schema or owner identity was incomplete: $canonical"
    }
    if ($ExpectedRunId -and [string]$snapshot.owner_run_id -cne [string]$ExpectedRunId) {
        throw "Install snapshot owner identity did not match the current run: $canonical"
    }
    return $snapshot
}

function Restore-InstallSnapshot {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory = $true)][string]$SnapshotPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSnapshotSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedSnapshotIdentity,
        [Parameter(Mandatory = $true)][string]$ExpectedRunId,
        [string]$CandidateRoot,
        [string]$ExpectedCandidateRootIdentity,
        [switch]$KeepCandidateRoot,
        [switch]$CandidateRootOwnedByRun,
        [switch]$RestoreTasks
    )
    $snapshot = Read-VerifiedInstallSnapshot -SnapshotPath $SnapshotPath -ExpectedSnapshotSha256 $ExpectedSnapshotSha256 -ExpectedSnapshotIdentity $ExpectedSnapshotIdentity -ExpectedRunId $ExpectedRunId
    if ([string]$snapshot.schema_version -ne 'hwpx/windows-install-snapshot/v1' -or
        [string]$snapshot.snapshot_schema -ne 'hwpx/windows-install-snapshot/v1' -or
        -not ($snapshot.PSObject.Properties.Name -contains 'tasks') -or
        -not ($snapshot.PSObject.Properties.Name -contains 'processes')) {
        throw "Rollback snapshot schema is incomplete or untrusted: $SnapshotPath"
    }
    if (@($snapshot.tasks).Count -gt $script:MaxSnapshotTasks -or @($snapshot.processes).Count -gt $script:MaxSnapshotProcesses) {
        throw "Rollback snapshot exceeds bounded task/process cardinality: $SnapshotPath"
    }
    if ($CandidateRoot -and (Test-Path -LiteralPath $CandidateRoot -PathType Container)) {
        if ([string]::IsNullOrWhiteSpace($ExpectedCandidateRootIdentity)) {
            if (-not $KeepCandidateRoot -and $CandidateRootOwnedByRun) {
                throw "Candidate root stable object identity is missing: $CandidateRoot"
            }
        }
        else {
            Assert-PathObjectIdentity -Path $CandidateRoot -ExpectedIdentity $ExpectedCandidateRootIdentity | Out-Null
        }
    }
    $preservedProcessIdentities = @()
    if ($snapshot.PSObject.Properties.Name -contains 'processes') {
        $preservedProcessIdentities = @($snapshot.processes)
    }
    $processReleaseBeforeTasks = if ($CandidateRoot) {
        Stop-InstallProcesses -RootPath $CandidateRoot -PreserveProcessIdentities $preservedProcessIdentities
    }
    else {
        [pscustomobject]@{ ok = $true; RootPath = $null; root = $null; snapshot_count = 0; stopped = @(); race_released_process_ids = @(); preserved_process_ids = @(); remaining = @() }
    }
    if (-not $processReleaseBeforeTasks.ok -or @($processReleaseBeforeTasks.remaining).Count -gt 0) {
        throw 'Candidate process handles were not fully released before rollback task restoration.'
    }
    if ($RestoreTasks) {
        foreach ($task in @($snapshot.tasks)) {
            $taskName = Assert-SafeScheduledTaskName -TaskName ([string]$task.task_name)
            $taskPath = Assert-CanonicalScheduledTaskPath -TaskPath ([string]$task.task_path)
            if ($task.exists -and [string]::IsNullOrWhiteSpace([string]$task.xml)) {
                throw "Rollback task snapshot has no XML: $taskName"
            }
            $shouldStopTask = $true
            if ($task.exists -and [string]$task.state -eq 'Running' -and $preservedProcessIdentities.Count -gt 0) {
                $shouldStopTask = $false
            }
            if ($taskName -and $shouldStopTask) {
                $currentTask = Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPath -AllowMissing
                if ($currentTask) {
                    Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
                    if (-not (Wait-ScheduledTaskInactive -TaskName $taskName -TaskPath $taskPath)) {
                        throw "Scheduled task remained active before rollback root cleanup: $taskName"
                    }
                }
            }
        }
    }
    if ($CandidateRoot -and (Test-Path -LiteralPath $CandidateRoot) -and (-not $KeepCandidateRoot) -and (-not $CandidateRootOwnedByRun)) {
        throw "Refusing to remove a candidate root without explicit current-run ownership: $CandidateRoot"
    }
    if ($RestoreTasks) {
        foreach ($task in @($snapshot.tasks)) {
            $taskName = Assert-SafeScheduledTaskName -TaskName ([string]$task.task_name)
            $taskPath = Assert-CanonicalScheduledTaskPath -TaskPath ([string]$task.task_path)
            if ($task.exists -and $task.xml) {
                $currentBeforeRestore = Get-ScheduledTaskIdentity -TaskName $taskName -TaskPath $taskPath
                $expectedCurrentIdentity = $null
                if ([bool]$currentBeforeRestore.exists) {
                    $candidateBinding = $false
                    if ($CandidateRoot -and $CandidateRootOwnedByRun -and (Test-Path -LiteralPath $CandidateRoot -PathType Container)) {
                        try {
                            $candidateRootCanonical = Get-CanonicalPath -Path $CandidateRoot -RequireExisting
                            $candidatePython = Join-Path $candidateRootCanonical '.venv\Scripts\python.exe'
                            $candidateBinding = Test-CanonicalTaskActionBinding -Identity $currentBeforeRestore -ExpectedRoot $candidateRootCanonical -ExpectedPythonPath $candidatePython
                        }
                        catch { $candidateBinding = $false }
                    }
                    $snapshotBinding = Test-ScheduledTaskIdentityExact -Actual $currentBeforeRestore -Expected $task
                    if (-not $snapshotBinding -and -not $candidateBinding) {
                        throw "Refusing to overwrite an unverified scheduled task during rollback: $taskName"
                    }
                    $expectedCurrentIdentity = $currentBeforeRestore
                }
                $restoredIdentity = Register-ScheduledTaskExactNoClobber -TaskName $taskName -TaskPath $taskPath -Xml ([string]$task.xml) -ExpectedCurrentIdentity $expectedCurrentIdentity
                $restoredTask = Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPath
                if ([string]$task.task_identity_hash -ne [string]$restoredIdentity.task_identity_hash) {
                    throw "Rollback task identity hash did not match the snapshot: $taskName"
                }
                if ([string]$task.xml -cne [string]$restoredIdentity.xml) {
                    throw "Rollback task XML did not match the snapshot: $taskName"
                }
                foreach ($identityField in @('execute', 'arguments', 'working_directory', 'action_type', 'action_count', 'xml_action_count', 'xml_exec_action_count', 'trigger_type', 'logon_type', 'run_level', 'start_when_available', 'multiple_instances_policy', 'execution_time_limit', 'restart_count', 'restart_interval')) {
                    $expectedIdentityValue = [string](Get-OptionalPropertyValue -Object $task -Name $identityField)
                    $actualIdentityValue = [string](Get-OptionalPropertyValue -Object $restoredIdentity -Name $identityField)
                    if ($expectedIdentityValue -ne $actualIdentityValue) {
                        throw "Rollback task identity field '$identityField' did not match the snapshot: $taskName"
                    }
                }
                if (-not (Test-WindowsPrincipalEquivalent -Actual ([string]$restoredIdentity.principal) -Expected ([string]$task.principal))) {
                    throw "Rollback task principal did not match the snapshot: $taskName"
                }
                if (-not ($restoredIdentity.PSObject.Properties.Name -contains 'enabled') -or [bool]$restoredIdentity.enabled -ne [bool]$task.enabled) {
                    throw "Rollback task enabled state did not match the snapshot: $taskName"
                }
                foreach ($settingName in @('MultipleInstancesPolicy', 'DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries', 'AllowHardTerminate', 'StartWhenAvailable', 'RunOnlyIfNetworkAvailable', 'Enabled', 'Hidden', 'ExecutionTimeLimit', 'Priority')) {
                    $expectedSetting = [string](Get-OptionalPropertyValue -Object $task.settings -Name $settingName)
                    $actualSetting = [string](Get-OptionalPropertyValue -Object $restoredIdentity.settings -Name $settingName)
                    if ($expectedSetting -ne $actualSetting) {
                        throw "Rollback task setting '$settingName' did not match the snapshot: $taskName"
                    }
                }
                if ([string]$task.state -eq 'Running' -and [string]$restoredTask.State -ne 'Running') {
                    Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
                }
                elseif ([string]$task.state -ne 'Running' -and [string]$restoredTask.State -eq 'Running') {
                    Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
                }
                $readbackTask = Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPath
                if ([string]$task.state -eq 'Running' -and [string]$readbackTask.State -ne 'Running') {
                    throw "Scheduled task state was not restored to Running: $taskName"
                }
                if ([string]$task.state -ne 'Running' -and [string]$readbackTask.State -eq 'Running') {
                    throw "Scheduled task state was not restored to $($task.state): $taskName"
                }
            }
            elseif (-not $task.exists) {
                $currentTask = Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPath -AllowMissing
                if ($currentTask) {
                    if ([string]::IsNullOrWhiteSpace($CandidateRoot)) {
                        throw "Refusing to remove a scheduled task without a current-run candidate root: $taskName"
                    }
                    $candidatePython = Join-Path (Get-CanonicalPath -Path $CandidateRoot -RequireExisting) '.venv\Scripts\python.exe'
                    $currentIdentity = Get-ScheduledTaskIdentity -TaskName $taskName -TaskPath $taskPath
                    if (-not (Test-CanonicalTaskActionBinding -Identity $currentIdentity -ExpectedRoot $CandidateRoot -ExpectedPythonPath $candidatePython)) {
                        throw "Refusing to remove an unverified scheduled task during rollback: $taskName"
                    }
                    Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction Stop
                }
                if (Get-ScheduledTaskExact -TaskName $taskName -TaskPath $taskPath -AllowMissing) {
                    throw "Scheduled task remained after rollback: $taskName"
                }
            }
        }
    }
    if ($CandidateRoot -and (Test-Path -LiteralPath $CandidateRoot) -and (-not $KeepCandidateRoot) -and $CandidateRootOwnedByRun -and $PSCmdlet.ShouldProcess($CandidateRoot, 'Remove candidate install root')) {
        if ([string]::IsNullOrWhiteSpace($ExpectedCandidateRootIdentity)) { throw "Candidate root stable object identity is missing: $CandidateRoot" }
        Assert-PathObjectIdentity -Path $CandidateRoot -ExpectedIdentity $ExpectedCandidateRootIdentity | Out-Null
        Remove-PathIdentityExact -Path $CandidateRoot -ExpectedObjectIdentity $ExpectedCandidateRootIdentity | Out-Null
        if (Test-Path -LiteralPath $CandidateRoot) { throw "Candidate install root remained after rollback cleanup: $CandidateRoot" }
    }
    $processReleaseAfterTasks = if ($CandidateRoot) {
        Stop-InstallProcesses -RootPath $CandidateRoot -PreserveProcessIdentities $preservedProcessIdentities
    }
    else {
        [pscustomobject]@{ ok = $true; RootPath = $null; root = $null; snapshot_count = 0; stopped = @(); race_released_process_ids = @(); preserved_process_ids = @(); remaining = @() }
    }
    $processesReleased = [bool]$processReleaseBeforeTasks.ok -and [bool]$processReleaseAfterTasks.ok
    return [pscustomobject]@{
        restored = $true
        snapshot_path = (Get-CanonicalPath -Path $SnapshotPath -RequireExisting)
        processes_released = $processesReleased
        process_release = [pscustomobject]@{
            before_tasks = $processReleaseBeforeTasks
            after_tasks = $processReleaseAfterTasks
        }
    }
}

Export-ModuleMember -Function Test-ProhibitedPrivateSourceMember, Test-ProhibitedSourceMember, Get-CanonicalPath, Get-PathObjectIdentity, Assert-PathObjectIdentity, Enter-InstallRootLock, Enter-MachineLifecycleLock, Enter-InstallLifecycleLock, Add-MachineLifecycleLockScope, Exit-InstallLifecycleLock, Exit-PathMutex, Enter-ReceiptPathLock, Assert-ReceiptPathAdmission, Get-InstallTransactionJournalPath, Write-StableTransactionJournal, Read-StableTransactionJournal, Assert-NoReparsePath, Assert-NoReparseSourcePath, Assert-WindowsSafeSourceRelativePath, Remove-PathIdentityExact, Move-PathIdentityExact, Get-Sha256Hex, Get-TextSha256, Get-OptionalPropertyValue, Copy-FileVerified, Test-NonEmptyFile, Invoke-NativeChecked, Get-SourceManifest, Get-ScheduledTaskIdentity, Get-ScheduledTaskExact, Test-ScheduledTaskIdentityExact, Stop-ScheduledTaskExactAndWait, Register-ScheduledTaskExactNoClobber, Assert-SafeScheduledTaskName, Assert-CanonicalScheduledTaskPath, Test-CanonicalTaskSettings, Test-CanonicalPathWithinRoot, Test-CommandLineModuleToken, Test-CommandLinePathToken, Test-CanonicalTaskActionBinding, Test-CanonicalProcessIdentity, Get-InstallProcessSnapshot, Get-ProcessGenerationIdentity, Stop-InstallProcesses, Wait-ScheduledTaskInactive, Wait-ScheduledTaskRunning, Resolve-WindowsPrincipalIdentity, Test-WindowsPrincipalEquivalent, Test-ScheduledTaskLogonTypeEquivalent, Test-ScheduledTaskRunLevelEquivalent, Write-JsonReceipt, Save-InstallSnapshot, Read-VerifiedInstallSnapshot, Restore-InstallSnapshot, Limit-Text, Read-BoundedText, Read-BoundedJsonObject, Get-ConfiguredEnvValue, Get-ConfiguredApiPort, Resolve-ApiPort
