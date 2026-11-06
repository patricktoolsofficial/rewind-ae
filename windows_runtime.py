"""Install the shared Microsoft renderer only when it is absent."""
import os
import subprocess
import tempfile
import urllib.request

RUNTIME_KEY = r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
BOOTSTRAPPER = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'


def runtime_installed():
    import winreg
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(root, RUNTIME_KEY, 0, winreg.KEY_READ | view) as key:
                    version, _ = winreg.QueryValueEx(key, 'pv')
                if isinstance(version, str) and any(int(n) for n in version.split('.')):
                    return True
            except (OSError, ValueError):
                continue
    return False


def ensure_runtime():
    if runtime_installed():
        return
    with tempfile.TemporaryDirectory(prefix='RewindAE-Setup-') as directory:
        installer = os.path.join(directory, 'MicrosoftEdgeWebview2Setup.exe')
        with urllib.request.urlopen(BOOTSTRAPPER, timeout=60) as response, open(installer, 'wb') as output:
            total = 0
            while chunk := response.read(65536):
                total += len(chunk)
                if total > 20 * 1024 * 1024:
                    raise RuntimeError('Unexpected WebView2 installer size')
                output.write(chunk)
        # Verify publisher and certificate before executing the downloaded installer.
        env = dict(os.environ, REWINDAE_SETUP_PATH=installer)
        powershell = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                  'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        verification = subprocess.run(
            [powershell, '-NoProfile', '-NonInteractive', '-Command',
             "$s=Get-AuthenticodeSignature -LiteralPath $env:REWINDAE_SETUP_PATH; "
             "if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(?:,|$)'){exit 1}"],
            env=env, capture_output=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if verification.returncode:
            raise RuntimeError('Microsoft installer signature could not be verified')
        result = subprocess.run([installer, '/silent', '/install'], timeout=600,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode not in (0, 3010) or not runtime_installed():
            raise RuntimeError('WebView2 setup did not finish. Check your connection or contact your administrator.')
