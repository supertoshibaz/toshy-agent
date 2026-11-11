#!/usr/bin/env bash
# Toshy Linux x86-64 bootstrap. No preinstalled Python is required.
set -euo pipefail
local_source=""
script_file="${BASH_SOURCE[0]:-}"
if [[ -n "$script_file" && -f "$script_file" ]]; then
    candidate="$(cd -- "$(dirname -- "$script_file")" && pwd)"
    [[ ! -f "$candidate/release_tools/cli.py" ]] || local_source="$candidate"
fi
root="${TOSHY_INSTALL_ROOT:-${local_source:-$PWD/toshy}}"
repo="${TOSHY_REPOSITORY:-supertoshibaz/toshy-agent}"
arguments=("$@")
unattended=0
for ((i=0; i<${#arguments[@]}; i++)); do
    case "${arguments[$i]}" in
        --root|--repository)
            option="${arguments[$i]}"
            value="${arguments[$((i+1))]:-}"
            [[ -n "$value" && "$value" != --* ]] || { echo "Missing $option value." >&2; exit 2; }
            if [[ "$option" == --root ]]; then root="$value"; else repo="$value"; fi
            i=$((i+1)) ;;
        --yes) unattended=1 ;;
        -h|--help)
            echo 'Usage: install.sh [--root DIRECTORY] [--repository OWNER/REPOSITORY]'
            echo '                  [--components git,docker,browser,ocr] [--yes]'
            echo '                  [--no-desktop] [--skip-system] [--prepare-only]'
            exit 0 ;;
    esac
done
[[ "$repo" =~ ^[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*$ ]] || { echo 'Use --repository OWNER/REPOSITORY.' >&2; exit 2; }
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || { echo 'Supported: Linux x86-64.' >&2; exit 2; }
while [[ "$root" == */ && "$root" != / ]]; do root="${root%/}"; done
[[ "$root" == /* && "$root" != / && "$root" != "$HOME" && ! -L "$root" ]] || { echo 'Choose an absolute, private installation directory.' >&2; exit 2; }
for tool in curl tar sha256sum; do
    command -v "$tool" >/dev/null || { echo "Install prerequisite: $tool" >&2; exit 2; }
done
# A piped script owns stdin. Never let Python read more installer code as
# answers. A controlling terminal still works when stdout is redirected.
install_input=""
if [[ $unattended -eq 1 ]]; then
    install_input=/dev/null
elif [[ ! -t 0 ]]; then
    if { : < /dev/tty; } 2>/dev/null; then
        install_input=/dev/tty
    else
        echo 'No interactive terminal. Use --yes and --components for unattended installation.' >&2
        exit 2
    fi
fi
if [[ -d "$root" && ! -f "$root/installation.json" && -n "$(ls -A "$root")" ]] && ! { [[ -f "$root/.toshy-bootstrap" ]] && [[ "$(cat "$root/.toshy-bootstrap")" == "$root" ]]; }; then
    [[ -f "$root/run_ui.py" && -f "$root/release-manifest.json" && -f "$root/release_tools/cli.py" ]] || { echo 'Choose an empty directory or the unpacked Toshy folder.' >&2; exit 2; }
fi
temporary="$(mktemp -d)"
# Never remove the agent root on failure: it may contain the user's project.
cleanup() { rm -rf -- "$temporary"; }
trap cleanup EXIT
trap 'echo "Installation interrupted." >&2; exit 130' INT
trap 'echo "Installation interrupted." >&2; exit 143' TERM HUP
for directory in "$root/tools" "$root/tools/python" "$root/cache" "$root/cache/uv" "$root/tools/uv" "$root/tools/uv-version.json" "$root/.toshy-bootstrap"; do
    [[ ! -L "$directory" ]] || { echo "Installation directory cannot be a link: $directory" >&2; exit 2; }
done
mkdir -p "$root/tools/python" "$root/cache/uv"
printf '%s\n' "$root" > "$root/.toshy-bootstrap"
export UV_PYTHON_INSTALL_DIR="$root/tools/python" UV_CACHE_DIR="$root/cache/uv" UV_NO_CONFIG=1
export PYTHONUNBUFFERED=1
unset UV_NO_PROGRESS
archive="$temporary/uv.tar.gz"
echo 'Preparing private Python. Downloads retry while the connection is unavailable; Ctrl+C cancels.'
if [[ ! -x "$root/tools/uv" ]] || [[ ! "$("$root/tools/uv" --version)" =~ ^uv[[:space:]]0\.13\.7([[:space:]]|$) ]]; then
    curl -fL --progress-bar --retry 100000 --retry-all-errors --retry-delay 5 --connect-timeout 15 --max-time 600 \
        'https://github.com/astral-sh/uv/releases/download/0.13.7/uv-x86_64-unknown-linux-gnu.tar.gz' -o "$archive"
    echo 'c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83  '"$archive" | sha256sum -c -
    tar -xzf "$archive" -C "$temporary" uv-x86_64-unknown-linux-gnu/uv
    cp "$temporary/uv-x86_64-unknown-linux-gnu/uv" "$root/tools/uv"
    chmod 755 "$root/tools/uv"
    printf '%s\n' '{"version":"0.13.7","archive_sha256":"c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83"}' > "$root/tools/uv-version.json"
fi
while true; do
    if "$root/tools/uv" python install 3.13.17 2>&1 | tee "$temporary/python.log"; then break; fi
    if ! grep -Eiq 'connection|timed? out|temporary failure|failed to download|dns|network is unreachable' "$temporary/python.log"; then exit 2; fi
    echo 'Waiting for the Python download; Ctrl+C cancels.' >&2
    sleep 5
done
interpreter="$("$root/tools/uv" python find --managed-python 3.13.17)"
if [[ -z "$local_source" ]]; then
    "$interpreter" - "$repo" "$temporary/source" <<'PY'
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
PY
    local_source="$temporary/source"
fi
# install.py handles --root regardless of its position and verifies the release.
if [[ -n "$install_input" ]]; then
    "$interpreter" "$local_source/install.py" --root "$root" --repository "$repo" "${arguments[@]}" < "$install_input"
else
    "$interpreter" "$local_source/install.py" --root "$root" --repository "$repo" "${arguments[@]}"
fi
echo 'Open Toshy from the application menu or a new terminal: toshy'
