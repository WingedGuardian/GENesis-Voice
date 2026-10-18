// Rendered by setup.ps1; installation paths never pass through cmd interpolation.
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const packageRoot = __PACKAGE_ROOT__;

try {
  let root = packageRoot;
  const manifestAt = p => path.join(p, 'codex-resources', 'voice', 'manifest.json');
  if (!fs.existsSync(manifestAt(root))) {
    const vendor = path.join(root, 'node_modules', '@openai', `codex-win32-${process.arch}`, 'vendor');
    const candidates = fs.readdirSync(vendor).map(p => path.join(vendor, p))
      .filter(p => fs.existsSync(manifestAt(p)));
    if (candidates.length !== 1) throw Error('Cannot identify the native Windows Codex package');
    root = candidates[0];
  }
  const manifest = JSON.parse(fs.readFileSync(manifestAt(root), 'utf8'));
  const helper = path.join(root, 'codex-resources', 'voice', 'bin', 'codex-voice-host.exe');
  const commit = spawnSync(helper, ['--build-commit'], {encoding: 'utf8', windowsHide: true});
  if (commit.error || commit.status !== 0 || commit.stdout.trim() !== manifest.buildCommit) {
    throw Error('Windows helper does not match its package manifest');
  }
  if (process.argv.includes('--describe')) {
    console.log(JSON.stringify({buildCommit: manifest.buildCommit, appVersion: manifest.appVersion}));
  } else {
    // Mirror the packaged client's environment allowlist, then select its private runtime.
    const keys = /^(SYSTEMROOT|WINDIR|HOME|USERPROFILE|LOCALAPPDATA|APPDATA|TEMP|TMP|TMPDIR|XDG_RUNTIME_DIR|PULSE_SERVER|PULSE_COOKIE|PIPEWIRE_REMOTE|DBUS_SESSION_BUS_ADDRESS|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|SSL_CERT_FILE|SSL_CERT_DIR|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE)$/;
    const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => keys.test(k.toUpperCase())));
    Object.assign(env, {GST_PLUGIN_PATH: '', GST_PLUGIN_PATH_1_0: '', GST_PLUGIN_SYSTEM_PATH: '',
      GST_PLUGIN_SYSTEM_PATH_1_0: '', GST_REGISTRY: 'NUL', GST_REGISTRY_UPDATE: 'no', GST_REGISTRY_FORK: 'no'});
    const child = spawnSync(helper, [], {stdio: 'inherit', windowsHide: true, env});
    if (child.error) throw child.error;
    process.exitCode = child.status ?? 1;
  }
} catch (error) {
  console.error('Codex voice helper: ' + error.message);
  process.exitCode = 1;
}
