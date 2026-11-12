[CmdletBinding()]
param(
    [string]$Root = $env:TOSHY_INSTALL_ROOT,
    [string]$Repository = $(if ($env:TOSHY_REPOSITORY) { $env:TOSHY_REPOSITORY } else { "supertoshibaz/toshy-agent" }),
    [string]$Components = "",
    [switch]$Yes,
    [switch]$NoDesktop,
    [switch]$SkipSystem
)
# A child scope keeps helper functions and error preferences out of the shell
# when this file is evaluated with iex (irm ...). {}.File is empty under iex,
# even if the caller itself is another .ps1 file.
& {
    param([hashtable]$Bootstrap)
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    $Root = $Bootstrap.Root
    if (-not $Root) {
        $Root = Join-Path (Get-Location).Path 'toshy'
        if ($Bootstrap.ScriptFile) {
            $Candidate = Split-Path -Parent $Bootstrap.ScriptFile
            if (Test-Path -LiteralPath (Join-Path $Candidate 'release_tools\cli.py')) { $Root = $Candidate }
        }
    }
    $Repository = $Bootstrap.Repository
    $OSArchitecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    if ($OSArchitecture -ne 'X64') { throw "Supported: Windows x64. Detected: $OSArchitecture." }
    if ($Repository -notmatch '^[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*$') { throw 'Use -Repository OWNER/REPOSITORY.' }
    $Root = [IO.Path]::GetFullPath($Root)
    if ($Root -eq [IO.Path]::GetPathRoot($Root) -or $Root -eq $HOME) { throw 'Choose a private installation directory.' }
    if (Test-Path -LiteralPath $Root) {
        if ((Get-Item -LiteralPath $Root).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Installation directory cannot be a link.' }
        if (-not (Test-Path -LiteralPath (Join-Path $Root 'installation.json')) -and (Get-ChildItem -Force -LiteralPath $Root | Measure-Object).Count -gt 0 -and -not ((Test-Path -LiteralPath (Join-Path $Root '.toshy-bootstrap')) -and (Get-Content -Raw -LiteralPath (Join-Path $Root '.toshy-bootstrap')).Trim() -eq $Root) -and -not ((Test-Path -LiteralPath (Join-Path $Root 'run_ui.py')) -and (Test-Path -LiteralPath (Join-Path $Root 'release-manifest.json')) -and (Test-Path -LiteralPath (Join-Path $Root 'release_tools\cli.py')))) { throw 'Choose an empty directory or the unpacked Toshy folder.' }
    }
    $Temporary = Join-Path ([IO.Path]::GetTempPath()) ('toshy-bootstrap-' + [Guid]::NewGuid())
    $PreviousEnvironment = @{}
    foreach ($Key in @('UV_PYTHON_INSTALL_DIR','UV_CACHE_DIR','UV_NO_PROGRESS','UV_NO_CONFIG','PYTHONUNBUFFERED')) {
        $PreviousEnvironment[$Key] = [Environment]::GetEnvironmentVariable($Key, 'Process')
    }
    function Download([string]$Url,[string]$Destination) {
        while ($true) {
            $Response = $null; $InputStream = $null; $File = $null
            try {
                $Request = [Net.WebRequest]::Create($Url)
                $Request.Timeout = 120000; $Request.ReadWriteTimeout = 120000
                $Response = $Request.GetResponse()
                $Total = $Response.ContentLength; $Count = 0L
                $InputStream = $Response.GetResponseStream()
                $File = [IO.File]::Create($Destination)
                $Buffer = New-Object byte[] 65536
                $LastDraw = [DateTime]::MinValue
                $Name = [IO.Path]::GetFileName($Destination)
                Write-Host "Downloading $Name..."
                while (($Read = $InputStream.Read($Buffer,0,$Buffer.Length)) -gt 0) {
                    $File.Write($Buffer,0,$Read); $Count += $Read
                    if (-not [Console]::IsOutputRedirected -and (([DateTime]::UtcNow - $LastDraw).TotalMilliseconds -ge 100)) {
                        if ($Total -gt 0) {
                            $Percent = [Math]::Min(100,[int]($Count * 100 / $Total))
                            $Filled = [int][Math]::Floor($Percent / 5)
                            Write-Host -NoNewline ("`r$Name [" + ('=' * $Filled) + (' ' * (20-$Filled)) + "] $Percent%   ")
                        } else { Write-Host -NoNewline ("`r$Name  {0:N1} MiB   " -f ($Count / (1024.0 * 1024.0))) }
                        $LastDraw = [DateTime]::UtcNow
                    }
                }
                $File.Flush()
                if ($Total -ge 0 -and $Count -ne $Total) { throw [IO.IOException]::new('Download interrupted.') }
                if (-not [Console]::IsOutputRedirected) { Write-Host ("`r$Name [====================] 100%   ") }
                Write-Host "Downloaded $Name"
                return
            } catch {
                $Status = 0
                if ($_.Exception.Response) { $Status = [int]$_.Exception.Response.StatusCode }
                if ($Status -ge 400 -and $Status -notin @(408,429,500,502,503,504)) { throw }
                Write-Host 'Waiting for connection; Ctrl+C cancels.'; Start-Sleep -Seconds 5
            } finally {
                if ($File) { $File.Dispose() }
                if ($InputStream) { $InputStream.Dispose() }
                if ($Response) { $Response.Dispose() }
            }
        }
    }
    try {
        foreach ($Directory in @('tools','tools\python','cache','cache\uv','tools\uv.exe','tools\uv-version.json','.toshy-bootstrap')) {
            $Path = Join-Path $Root $Directory
            if ((Test-Path -LiteralPath $Path) -and ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Installation resources cannot be links.' }
        }
        New-Item -ItemType Directory -Force -Path $Temporary,(Join-Path $Root 'tools\python'),(Join-Path $Root 'cache\uv') | Out-Null
        Set-Content -LiteralPath (Join-Path $Root '.toshy-bootstrap') -Value $Root -Encoding UTF8
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $Root 'tools\python'
        $env:UV_CACHE_DIR = Join-Path $Root 'cache\uv'
        $env:UV_NO_PROGRESS = $null
        $env:PYTHONUNBUFFERED = '1'
        $env:UV_NO_CONFIG = '1'
        $Uv = Join-Path $Root 'tools\uv.exe'
        $UvReady = $false
        if (Test-Path -LiteralPath $Uv) { $UvReady = ((& $Uv --version) -match '^uv 0[.]13[.]7(?:\s|$)') }
        if (-not $UvReady) {
        $Archive = Join-Path $Temporary 'uv.zip'
        Download 'https://github.com/astral-sh/uv/releases/download/0.13.7/uv-x86_64-pc-windows-msvc.zip' $Archive
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant() -ne '0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021') { throw 'uv checksum mismatch.' }
        Expand-Archive -LiteralPath $Archive -DestinationPath (Join-Path $Temporary 'uv')
        $UvSource = Get-ChildItem -LiteralPath (Join-Path $Temporary 'uv') -Recurse -Filter uv.exe | Select-Object -First 1
        if (-not $UvSource) { throw 'uv executable is missing from the download.' }
        $Uv = Join-Path $Root 'tools\uv.exe'
        Copy-Item -LiteralPath $UvSource.FullName -Destination $Uv -Force
        @{version='0.13.7';archive_sha256='0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021'} | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'tools\uv-version.json')
        }
        do {
            # Windows PowerShell 5.1 turns redirected native stderr into error
            # records. Capture it for retry classification without aborting early.
            $Preference = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                $Output = & $Uv python install 3.13.17 2>&1 | ForEach-Object { Write-Host $_; $_ }
                $Result = $LASTEXITCODE
            } finally { $ErrorActionPreference = $Preference }
            if ($Result -ne 0) {
                if (($Output -join "`n") -notmatch 'connection|timed? out|temporary failure|failed to download|dns|network is unreachable') { throw 'Python preparation failed.' }
                Write-Host 'Waiting for the Python download; Ctrl+C cancels.'; Start-Sleep 5
            }
        } while ($Result -ne 0)
        $Python = (& $Uv python find --managed-python 3.13.17 | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0 -or -not $Python -or -not (Test-Path -LiteralPath $Python)) { throw 'Python was not installed.' }
        $Source = ''
        if ($Bootstrap.ScriptFile) {
            $Candidate = Split-Path -Parent $Bootstrap.ScriptFile
            if (Test-Path -LiteralPath (Join-Path $Candidate 'release_tools\cli.py')) { $Source = $Candidate }
        }
        if (-not $Source) {
            $FetchSource = @'
import io,json,pathlib,re,stat,sys,time,urllib.request,urllib.error,zipfile
repo,destination=sys.argv[1],pathlib.Path(sys.argv[2])
def get(url,limit):
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Toshy-bootstrap/1'}),timeout=20) as response:
                total=int(getattr(response,'headers',{}).get('Content-Length',0))
                archive='/zipball/' in url
                if archive: print('Downloading Toshy source…',flush=True)
                chunks=[]; count=0; last=0
                while True:
                    block=response.read(min(256*1024,limit-count+1))
                    if not block: break
                    chunks.append(block); count+=len(block)
                    if count>limit: raise SystemExit('Bootstrap response exceeds size limit.')
                    if archive and sys.stdout.isatty() and time.monotonic()-last>=0.1:
                        if total:
                            percent=min(100,int(count*100/total)); filled=percent//5
                            print('\rToshy source ['+'='*filled+' '*(20-filled)+f'] {percent:3d}%',end='',flush=True)
                        else: print(f'\rToshy source  {count/1048576:.1f} MiB',end='',flush=True)
                        last=time.monotonic()
                if archive:
                    if sys.stdout.isatty(): print(flush=True)
                    print('Toshy source downloaded.',flush=True)
            return b''.join(chunks)
        except urllib.error.HTTPError as error:
            if error.code not in (408,429,500,502,503,504): raise SystemExit(f'GitHub HTTP {error.code}. Repository must be public and have a stable release.')
            print('Waiting for GitHub; Ctrl+C cancels.',flush=True);time.sleep(5)
        except (urllib.error.URLError,TimeoutError,ConnectionError):
            print('Waiting for connection; Ctrl+C cancels.',flush=True);time.sleep(5)
release=json.loads(get(f'https://api.github.com/repos/{repo}/releases/latest',8*1024**2))
tag=release.get('tag_name','')
if release.get('draft') or release.get('prerelease') or not re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',tag):
    raise SystemExit('GitHub must have a stable vMAJOR.MINOR.PATCH release.')
from urllib.parse import quote
tag_path=quote(tag,safe='')
commit=json.loads(get(f'https://api.github.com/repos/{repo}/commits/{tag_path}',8*1024**2))['sha']
if not isinstance(commit,str) or not re.fullmatch('[0-9a-f]{40}',commit): raise SystemExit('Invalid release commit.')
data=get(f'https://api.github.com/repos/{repo}/zipball/{commit}',512*1024**2)
with zipfile.ZipFile(io.BytesIO(data)) as archive:
    total=0
    prefix=None
    if len(archive.infolist())>20000: raise SystemExit('Source archive has too many files.')
    for entry in archive.infolist():
        p=pathlib.PurePosixPath(entry.filename)
        if p.is_absolute() or '..' in p.parts or '\\' in entry.filename or ':' in entry.filename or stat.S_ISLNK(entry.external_attr>>16): raise SystemExit('Unsafe source archive.')
        if not p.parts or len(p.parts)<2 and not entry.is_dir(): raise SystemExit('Invalid source archive layout.')
        if prefix is None: prefix=p.parts[0]
        if p.parts[0]!=prefix: raise SystemExit('Source archive must have one root directory.')
        total+=entry.file_size
        if total>1024**3: raise SystemExit('Source archive too large.')
        out=destination.joinpath(*p.parts[1:])
        if entry.is_dir(): out.mkdir(parents=True,exist_ok=True)
        else:
            out.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(entry) as src,out.open('xb') as dst:
                import shutil;shutil.copyfileobj(src,dst)
config=json.loads((destination/'release.json').read_text(encoding='utf-8'))
if config.get('version')!=tag.removeprefix('v'): raise SystemExit('Release tag and source version differ.')
if not (destination/'install.py').is_file() or not (destination/'release_tools/cli.py').is_file(): raise SystemExit('Release installer is missing.')
'@
            & $Python -c $FetchSource $Repository (Join-Path $Temporary 'source')
            if ($LASTEXITCODE -ne 0) { throw 'Could not download a valid stable Toshy release.' }
            $Source = Join-Path $Temporary 'source'
        }
        $Arguments = @((Join-Path $Source 'install.py'),'--root',$Root,'--repository',$Repository)
        if ($Bootstrap.HasComponents) { $Arguments += @('--components',$Bootstrap.Components) }
        if ($Bootstrap.Yes) { $Arguments += '--yes' }
        if ($Bootstrap.NoDesktop) { $Arguments += '--no-desktop' }
        if ($Bootstrap.SkipSystem) { $Arguments += '--skip-system' }
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Installation failed (exit $LASTEXITCODE). See $Root\install.log" }
        Write-Host 'Open Toshy from Start, Desktop or a new terminal: toshy'
    }
    finally {
        try {
            Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
            # Keep source, user data and reusable downloads after failure.
        } finally {
            foreach ($Key in $PreviousEnvironment.Keys) {
                [Environment]::SetEnvironmentVariable($Key, $PreviousEnvironment[$Key], 'Process')
            }
        }
    }
} @{
    Root = $Root
    Repository = $Repository
    Components = $Components
    HasComponents = $PSBoundParameters.ContainsKey('Components')
    Yes = $Yes.IsPresent
    NoDesktop = $NoDesktop.IsPresent
    SkipSystem = $SkipSystem.IsPresent
    ScriptFile = [string]{ $null }.File
}
