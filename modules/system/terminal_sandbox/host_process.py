"""Portable child process ownership, shell wrappers and path completion."""
import base64
import os
import shlex
import shutil


def ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def powershell(script, executable='powershell'):
    return [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand',
            base64.b64encode(script.encode('utf-16-le')).decode('ascii')]


def shell(profile):
    windows = (profile.get('target_os') == 'windows') if profile['backend'] == 'ssh' else (profile['backend'] == 'local' and os.name == 'nt')
    configured = profile.get('shell')
    if configured:
        return configured, windows
    return (('powershell' if profile['backend'] == 'ssh' else (shutil.which('pwsh') or 'powershell.exe')) if windows else '/bin/bash'), windows


def direct_command(command):
    """Whether a command can replace the wrapper without changing shell syntax."""
    try:
        lexer=shlex.shlex(command,posix=True,punctuation_chars=True);lexer.whitespace_split=True
        words=list(lexer)
    except ValueError:return False
    builtins={'cd','read','export','unset','alias','unalias','source','.','eval','exec','exit','set','trap','if','for','while','until','case','function'}
    return bool(words and words[0] not in builtins and '=' not in words[0] and
                not any(token and all(c in ';&|()<>' for c in token) for token in words) and
                not any(c in command for c in ('\n','`','$(')))


def wrapper(profile, job, control, *, gate=False):
    executable, windows = shell(profile)
    command, cwd = job['command'], job['cwd']
    if windows:
        prefix = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$OutputEncoding=[Console]::OutputEncoding;"
                  "$ErrorActionPreference='Continue';$global:LASTEXITCODE=0;")
        script = prefix + "$__toshyCode=0;"
        if gate:
            script += 'while(!(Test-Path -LiteralPath ' + ps_quote(control + '.gate') + ')){Start-Sleep -Milliseconds 10};'
        script += ("Set-Location -LiteralPath " + ps_quote(cwd) + " -ErrorAction Stop;"
                   "[IO.File]::WriteAllText(" + ps_quote(control + '.pid') + ",[string]$PID);try{\n" + command +
                   "\nif(!$?){$__toshyCode=1};if($LASTEXITCODE){$__toshyCode=$LASTEXITCODE}}catch{Write-Error $_;$__toshyCode=1}finally{"
                   "[IO.File]::WriteAllText(" + ps_quote(control + '.cwd') + ",(Get-Location).Path,[Text.UTF8Encoding]::new($false))};exit $__toshyCode")
        args = powershell(script, executable)
        if job.get('interactive'):
            args.remove('-NonInteractive')
        return args
    enter = 'builtin cd -- ' + shlex.quote(cwd)
    if job.get('inherit_cwd'):
        enter += ' || builtin cd -- ' + shlex.quote(job['base_cwd'])
    trap = 'builtin pwd -P > ' + shlex.quote(control + '.cwd')
    if job.get('interactive'):
        if job.get('tty') and direct_command(command):
            # Let the application own the PTY session. In particular an
            # interactive shell must receive SIGHUP and clean up its own jobs.
            script=(enter+' || exit $?; printf "%s\\n" "$$" > '+shlex.quote(control+'.pid')+
                    '; '+trap+'; exec '+command)
            return [executable,'-c',script]
        script = (enter + ' || exit $?; printf "%s\\n" "$$" > ' + shlex.quote(control + '.pid') +
                  '; trap ' + shlex.quote(trap) + ' EXIT;\n' + command)
        return [executable, '-c', script]
    script = ('set -m; ( ' + enter + ' || exit $?; trap ' + shlex.quote(trap) + ' EXIT;\n' + command +
              '\n) & __toshyGroup=$!; printf "%s\\n" "$__toshyGroup" > ' + shlex.quote(control + '.pid') +
              '; set +m; wait "$__toshyGroup"; exit $?')
    return [executable, '-c', script]


def remote_command(args):
    # A bare launcher works under both default cmd.exe and PowerShell sshd shells.
    if '-EncodedCommand' in args:
        if any(char.isspace() for char in args[0]):
            script = '& ' + ' '.join(ps_quote(a) for a in args) + '; exit $LASTEXITCODE'
            return ' '.join(powershell(script))
        return ' '.join(args)
    return shlex.join(args)


def clean_environment(extra=None):
    # Keep the login environment usable without forwarding Toshy's provider credentials.
    sensitive = ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY', 'AUTH', 'TOSHY_', 'OPENAI_', 'ANTHROPIC_')
    login = {'SSH_AUTH_SOCK','SSH_AGENT_PID','XAUTHORITY'}
    result = {k: v for k, v in os.environ.items() if k in login or not any(p in k.upper() for p in sensitive)}
    result.update(extra or {})
    return result


from core.process_owner import WindowsJob


def completion_script(profile, line, position, cwd):
    executable, windows = shell(profile)
    if windows:
        script = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);Set-Location -LiteralPath " + ps_quote(cwd) +
                  ";$r=TabExpansion2 -inputScript " + ps_quote(line) + " -cursorColumn " + str(len(line[:position].encode('utf-16-le'))//2) +
                  ";@{start=$r.ReplacementIndex;length=$r.ReplacementLength;items=@($r.CompletionMatches|Select-Object -First 100|ForEach-Object {$_.CompletionText})}|ConvertTo-Json -Compress")
        return powershell(script, executable)
    # Prefix is never evaluated as shell code. Completion is built from directory entries and PATH.
    prefix = line[:position]
    start, active_quote, escaped = 0, '', False
    for index, char in enumerate(prefix):
        if escaped:
            escaped = False
        elif char == '\\' and active_quote != "'":
            escaped = True
        elif active_quote:
            if char == active_quote: active_quote = ''
        elif char in ("'", '"'):
            active_quote = char
        elif char.isspace() or char in ';|&':
            start = index + 1
    raw = prefix[start:]
    quote = raw[0] if raw.startswith(('"', "'")) else ''
    word = raw[1:] if quote else raw
    if quote and word.endswith(quote):
        word = word[:-1]
    first = not prefix[:start].strip() or prefix[:start].rstrip().endswith((';', '|', '&&', '||'))
    script = 'builtin cd -- ' + shlex.quote(cwd) + ' || exit $?; '
    script += ('n=0; while IFS= read -r v; do printf "%s\\0" "$v"; ((++n>=100)) && break; done < <(compgen -A command -- '
               + shlex.quote(word) + '); ') if first and '/' not in word else ''
    # NUL-separated filenames preserve spaces; use builtins, no configured completion hooks.
    script += ('p=' + shlex.quote(word) + '; [[ "$p" == "~/"* ]] && p="$HOME/${p:2}"; '
               'n=0; while IFS= read -r v; do [[ -d "$v" ]] && v="$v/"; printf "%s\\0" "$v"; ((++n>=100)) && break; done < <(compgen -f -- "$p")')
    return [executable, '-c', script], {'start': start, 'length': position - start, 'quote': quote, 'prefix': word}
