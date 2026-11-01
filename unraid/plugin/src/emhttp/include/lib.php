<?php
declare(strict_types=1);
/*
 * Polyseat for Unraid - WebUI library. WIP / DO NOT USE.
 *
 * No output and no side effects at include time. The WebUI never builds a shell command:
 * it calls polyseatctl through proc_open() with an argument array that must pass the
 * allowlist below, and every state-changing request needs a same-origin POST, a CSRF token
 * (fail-closed), a typed confirmation phrase where destructive, and the controller's own
 * release gate. No listener is opened; pages are served by Unraid's authenticated emhttp.
 */

const PSU_PLUGIN_DIR = '/usr/local/emhttp/plugins/polyseat-unraid';

function psu_ctl_path(): string { return defined('PSU_CTL') ? PSU_CTL : PSU_PLUGIN_DIR . '/scripts/polyseatctl'; }
function psu_schema_path(): string { return defined('PSU_SCHEMA') ? PSU_SCHEMA : PSU_PLUGIN_DIR . '/schema.json'; }
function psu_var_ini_path(): string { return defined('PSU_VAR_INI') ? PSU_VAR_INI : '/var/local/emhttp/var.ini'; }

/* ---------------------------------------------------------------- output ---- */

/** Scalar-only string conversion for request data: arrays and objects become ''. */
function psu_str($value): string
{
    return is_string($value) || is_int($value) ? (string)$value : '';
}

function psu_e($value): string
{
    return htmlspecialchars((string)$value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

/** JSON that is safe inside an inline <script> (no </script>, no quote breakouts). */
function psu_json_inline($value): string
{
    return (string)json_encode($value, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT | JSON_UNESCAPED_SLASHES);
}

function psu_asset(string $relative): string
{
    $file = (defined('PSU_ASSET_DIR') ? PSU_ASSET_DIR : PSU_PLUGIN_DIR) . '/' . $relative;
    $version = is_file($file) ? (string)filemtime($file) : '0';
    return '/plugins/polyseat-unraid/' . $relative . '?v=' . rawurlencode($version);
}

/** @return array{name:string,light:bool} theme from Unraid's display settings, whitelisted. */
function psu_theme(array $display): array
{
    $name = strtolower((string)($display['theme'] ?? 'black'));
    if (!in_array($name, ['black', 'white', 'azure', 'gray'], true)) {
        $name = 'black';
    }
    return ['name' => $name, 'light' => in_array($name, ['white', 'azure'], true)];
}

/* ---------------------------------------------------------------- schema ---- */

function psu_schema(): array
{
    static $cache = [];
    $path = psu_schema_path();
    if (!isset($cache[$path])) {
        $data = json_decode((string)@file_get_contents($path), true);
        if (!is_array($data) || $data === []) {
            throw new RuntimeException('settings schema unavailable');
        }
        $cache[$path] = $data;
    }
    return $cache[$path];
}

/** Mirrors polyseatctl's schema_validate (same schema.json). */
function psu_validate_setting(string $key, string $value): bool
{
    $schema = psu_schema();
    if (!isset($schema[$key])) {
        return false;
    }
    $rule = $schema[$key];
    switch ($rule['type'] ?? '') {
        case 'enum':
            return in_array($value, $rule['values'], true);
        case 'int':
            return preg_match('/^[0-9]{1,10}$/D', $value) === 1
                && (int)$value >= $rule['min'] && (int)$value <= $rule['max'];
        case 'path':
        case 'string':
            return $value === '' || preg_match('~' . $rule['pattern'] . '~D', $value) === 1;
    }
    return false;
}

/* ------------------------------------------------------------ request auth ---- */

function psu_csrf_expected(): ?string
{
    $text = @file_get_contents(psu_var_ini_path());
    if ($text !== false && preg_match('/^csrf_token="?([A-Za-z0-9]{8,128})"?\s*$/m', $text, $m) === 1) {
        return $m[1];
    }
    return null;
}

/** Fail-closed: a missing token on either side is a rejection. */
function psu_csrf_ok(?string $submitted, ?string $expected): bool
{
    return is_string($submitted) && $submitted !== '' && is_string($expected) && $expected !== ''
        && hash_equals($expected, $submitted);
}

/** Same-origin check; without Origin and Referer the request is rejected. */
function psu_origin_ok(array $server): bool
{
    $host = strtolower((string)($server['HTTP_HOST'] ?? ''));
    if ($host === '') {
        return false;
    }
    foreach (['HTTP_ORIGIN', 'HTTP_REFERER'] as $header) {
        if (!empty($server[$header])) {
            $parts = parse_url((string)$server[$header]);
            if (!is_array($parts) || empty($parts['host'])) {
                return false;
            }
            $origin = strtolower($parts['host']) . (isset($parts['port']) ? ':' . $parts['port'] : '');
            return hash_equals($host, $origin);
        }
    }
    return false;
}

/* ------------------------------------------------------- controller calls ---- */

const PSU_READ_COMMANDS = ['status', 'metrics', 'list', 'config-get', 'logs', 'diagnose', 'preflight', 'health', 'verify',
                           'polyseatd-status', 'play-status'];
const PSU_WRITE_COMMANDS = ['start', 'stop', 'restart', 'activate', 'rollback', 'recover', 'config-set', 'config-reset',
                            'play-start', 'play-stop', 'play-recover'];

/** Allowlist for every argument vector the WebUI may ever pass to polyseatctl. */
function psu_ctl_args_ok(array $args): bool
{
    if ($args === [] || !array_is_list($args)) {
        return false;
    }
    foreach ($args as $arg) {
        if (!is_string($arg) || $arg === '' || strlen($arg) > 300 || preg_match('/[\x00-\x1f\x7f]/', $arg)) {
            return false;
        }
    }
    $cmd = $args[0];
    $rest = array_slice($args, 1);
    switch ($cmd) {
        case 'status':
            return $rest === [] || $rest === ['--json'];
        case 'metrics': case 'list': case 'config-get': case 'diagnose': case 'preflight': case 'health':
        case 'polyseatd-status': case 'play-status':
        case 'start': case 'stop': case 'restart': case 'config-reset':
        case 'play-start': case 'play-stop': case 'play-recover': // no --approve and no --wait from the web: the controller writes its own approval
            return $rest === [];
        case 'verify':
            return $rest === [] || (count($rest) === 1 && preg_match('/^[0-9a-f]{64}$/D', $rest[0]) === 1);
        case 'logs':
            return count($rest) === 2 && in_array($rest[0], ['controller', 'incusd'], true)
                && preg_match('/^[0-9]{1,3}$/D', $rest[1]) === 1 && (int)$rest[1] >= 1 && (int)$rest[1] <= 500;
        case 'activate':
            return count($rest) === 2 && preg_match('/^[0-9a-f]{64}$/D', $rest[0]) === 1 && $rest[1] === '--approve';
        case 'rollback': case 'recover':
            return $rest === ['--approve'];
        case 'config-set':
            if ($rest === []) {
                return false;
            }
            foreach ($rest as $pair) {
                if (preg_match('/^([A-Z_]+)=(.*)$/Ds', $pair, $m) !== 1 || !psu_validate_setting($m[1], $m[2])) {
                    return false;
                }
            }
            return true;
    }
    return false;
}

/**
 * What the Polyseat card may show of the controller's polyseatd-status file: a validated LAN address, whether the
 * daemon is alive, the seats' names and states, and when the play approval ends. Everything else is dropped.
 */
function psu_play_view(array $status): array
{
    $url = $status['url'] ?? null;
    $seats = null;
    if (is_array($status['seats'] ?? null)) {
        $seats = [];
        foreach (array_slice($status['seats'], 0, 64) as $seat) {
            if (is_array($seat) && is_string($seat['name'] ?? null) && is_string($seat['status'] ?? null)
                && preg_match('/^[a-z0-9][a-z0-9-]{0,62}$/D', $seat['name']) === 1
                && preg_match('/^[A-Za-z]{1,16}$/D', $seat['status']) === 1) {
                $seats[] = ['name' => $seat['name'], 'status' => $seat['status']];
            }
        }
    }
    return [
        'url' => is_string($url) && preg_match('~^https://[0-9]{1,3}(?:\.[0-9]{1,3}){3}:[0-9]{1,5}$~D', $url) === 1 ? $url : '',
        'alive' => ($status['alive'] ?? false) === true,
        'seats' => $seats,
        'expires_at' => is_int($status['expires_at'] ?? null) ? $status['expires_at'] : null,
    ];
}

/**
 * What the Polyseat card may know of the controller's play state (polyseatd-status prints one JSON line with a "state" and
 * exits 0 only while a session runs). A controller that predates the state prints nothing on exit 2: that is "idle".
 *
 * @return array{state:string,reason:?string,last_end:?array,status:?array}|null null = the controller could not be asked
 */
function psu_play_state(array $result): ?array
{
    $json = json_decode($result['out'], true);
    $json = is_array($json) ? $json : null;
    if ($result['code'] === 0) {
        return $json === null ? null : ['state' => 'running', 'reason' => null, 'last_end' => null, 'status' => $json];
    }
    if ($result['code'] !== 2) {
        return null;
    }
    $state = is_string($json['state'] ?? null) ? $json['state'] : 'idle';
    if (!in_array($state, ['idle', 'busy', 'needs-recovery'], true)) {
        return null;
    }
    $reason = null;
    if ($state === 'needs-recovery') {
        $r = $json['reason'] ?? null;
        $reason = is_string($r) && preg_match('/^[a-z][a-z0-9-]{0,63}$/D', $r) === 1 ? $r : 'unknown';
    }
    $end = $json['last_end'] ?? null;
    $lastEnd = null;
    if (is_array($end) && is_string($end['reason'] ?? null) && preg_match('/^[a-z][a-z0-9-]{0,63}$/D', $end['reason']) === 1
        && is_bool($end['clean'] ?? null) && is_int($end['ended_at'] ?? null)) {
        $lastEnd = ['reason' => $end['reason'], 'clean' => $end['clean'], 'recovered' => ($end['recovered'] ?? false) === true,
                    'ended_at' => $end['ended_at']];
    }
    return ['state' => $state, 'reason' => $reason, 'last_end' => $lastEnd, 'status' => null];
}

/** @return array{code:int,out:string,err:string,timeout:bool} */
function psu_run_ctl(array $args, int $timeoutSeconds = 60): array
{
    if (!psu_ctl_args_ok($args)) {
        return ['code' => 1, 'out' => '', 'err' => 'rejected by allowlist', 'timeout' => false];
    }
    $env = ['PATH' => '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG' => 'C'];
    foreach (['POLYSEAT_ROOT', 'POLYSEAT_CRASH_AT', 'POLYSEAT_FAKE_FREE_KB'] as $testVar) {
        if (defined('PSU_TEST_ENV') && isset(PSU_TEST_ENV[$testVar])) {
            $env[$testVar] = PSU_TEST_ENV[$testVar];
        }
    }
    $process = @proc_open([psu_ctl_path(), ...$args], [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, '/', $env);
    if (!is_resource($process)) {
        return ['code' => 127, 'out' => '', 'err' => 'controller not runnable', 'timeout' => false];
    }
    stream_set_blocking($pipes[1], false);
    stream_set_blocking($pipes[2], false);
    $out = $err = '';
    $deadline = microtime(true) + $timeoutSeconds;
    $timedOut = false;
    while (true) {
        $out .= (string)stream_get_contents($pipes[1]);
        $err .= (string)stream_get_contents($pipes[2]);
        $status = proc_get_status($process);
        if (!$status['running']) {
            break;
        }
        if (microtime(true) > $deadline) {
            proc_terminate($process, 15);
            usleep(300000);
            $timedOut = true;
            break;
        }
        if (strlen($out) > 1048576) {
            proc_terminate($process, 15);
            break;
        }
        usleep(50000);
    }
    $out .= (string)stream_get_contents($pipes[1]);
    $err .= (string)stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $code = proc_close($process);
    if (isset($status) && $status['running'] === false && isset($status['exitcode']) && $status['exitcode'] >= 0) {
        $code = $status['exitcode'];
    }
    return ['code' => $timedOut ? 124 : $code, 'out' => substr($out, 0, 1048576), 'err' => substr($err, 0, 65536), 'timeout' => $timedOut];
}

function psu_json_from(array $result): ?array
{
    if ($result['code'] !== 0) {
        return null;
    }
    $data = json_decode($result['out'], true);
    return is_array($data) ? $data : null;
}

/** Short, control-character-free operator hint taken from the controller's own error lines. */
function psu_detail(string $stderr): string
{
    $lines = array_values(array_filter(array_map('trim', explode("\n", $stderr))));
    $last = $lines === [] ? '' : (string)end($lines);
    $last = preg_replace('/[^\x20-\x7e]/', '?', $last) ?? '';
    return substr($last, 0, 300);
}

/* --------------------------------------------------------- confirmations ---- */

/** The phrase a human must type for a risky action. */
function psu_confirm_phrase(string $action, string $subject = ''): string
{
    switch ($action) {
        case 'activate': return 'ACTIVATE ' . substr($subject, 0, 8);
        case 'rollback': return 'ROLLBACK';
        case 'recover': return 'RECOVER';
        case 'stop': return 'STOP';
        case 'play_stop': return 'STOP';
        case 'play_recover': return 'WIEDERHERSTELLEN';
        case 'restart': return 'RESTART';
        case 'config_reset': return 'RESET';
    }
    return '';
}

/* --------------------------------------------------------------- handler ---- */

/**
 * @return array{0:int,1:array<string,string>,2:string} status, headers, JSON body
 */
function psu_handle(array $server, array $get, array $post): array
{
    $headers = ['Content-Type' => 'application/json; charset=utf-8', 'Cache-Control' => 'no-store',
                'X-Content-Type-Options' => 'nosniff'];
    $reply = function (int $status, array $body) use ($headers): array {
        return [$status, $headers, (string)json_encode($body, JSON_UNESCAPED_SLASHES)];
    };
    $method = psu_str($server['REQUEST_METHOD'] ?? '');

    if ($method === 'GET') {
        $action = psu_str($get['action'] ?? '');
        $map = [
            'status' => ['status', '--json'], 'metrics' => ['metrics'], 'list' => ['list'], 'config' => ['config-get'],
        ];
        if (isset($map[$action])) {
            $data = psu_json_from(psu_run_ctl($map[$action], 30));
            return $data === null ? $reply(502, ['ok' => false, 'error' => 'unavailable']) : $reply(200, ['ok' => true, 'data' => $data]);
        }
        if ($action === 'logs') {
            $which = psu_str($get['which'] ?? 'controller');
            $lines = psu_str($get['lines'] ?? '100');
            $result = psu_run_ctl(['logs', $which, $lines], 15);
            return $result['code'] === 0 ? $reply(200, ['ok' => true, 'data' => $result['out']]) : $reply(400, ['ok' => false, 'error' => 'params']);
        }
        if ($action === 'play') {
            // Read-only. The controller exits 2 when no play session runs and says why on stdout (idle, busy, or an
            // abnormal end that needs recovery): an answer, not a failure. "data" is the running session, as before.
            $state = psu_play_state(psu_run_ctl(['polyseatd-status'], 15));
            if ($state === null) {
                return $reply(502, ['ok' => false, 'error' => 'unavailable']);
            }
            return $reply(200, ['ok' => true, 'data' => $state['status'] === null ? null : psu_play_view($state['status']),
                                'state' => $state['state'], 'reason' => $state['reason'], 'last_end' => $state['last_end'],
                                'now' => time()]);
        }
        if ($action === 'diag') {
            $result = psu_run_ctl(['diagnose'], 30);
            return $reply(200, ['ok' => true, 'data' => $result['out'] . $result['err']]);
        }
        return $reply(400, ['ok' => false, 'error' => 'params']);
    }

    if ($method !== 'POST') {
        return $reply(405, ['ok' => false, 'error' => 'method']);
    }
    if (!psu_origin_ok($server)) {
        return $reply(403, ['ok' => false, 'error' => 'origin']);
    }
    if (!psu_csrf_ok(is_string($post['psu_csrf'] ?? null) ? $post['psu_csrf'] : null, psu_csrf_expected())) {
        return $reply(403, ['ok' => false, 'error' => 'csrf']);
    }

    $action = psu_str($post['action'] ?? '');
    $args = null;
    $phrase = '';
    switch ($action) {
        case 'service':
            $do = psu_str($post['do'] ?? '');
            if (!in_array($do, ['start', 'stop', 'restart'], true)) {
                return $reply(400, ['ok' => false, 'error' => 'params']);
            }
            $args = [$do];
            $phrase = $do === 'start' ? '' : psu_confirm_phrase($do);
            break;
        case 'activate':
            $sha = psu_str($post['sha'] ?? '');
            if (preg_match('/^[0-9a-f]{64}$/D', $sha) !== 1) {
                return $reply(400, ['ok' => false, 'error' => 'params']);
            }
            $args = ['activate', $sha, '--approve'];
            $phrase = psu_confirm_phrase('activate', $sha);
            break;
        case 'play_start':
            $args = ['play-start'];
            break;
        case 'play_stop':
            $args = ['play-stop'];
            $phrase = psu_confirm_phrase('play_stop');
            break;
        case 'play_recover':
            $args = ['play-recover'];
            $phrase = psu_confirm_phrase('play_recover');
            break;
        case 'rollback':
            $args = ['rollback', '--approve'];
            $phrase = psu_confirm_phrase('rollback');
            break;
        case 'recover':
            $args = ['recover', '--approve'];
            $phrase = psu_confirm_phrase('recover');
            break;
        case 'config_reset':
            $args = ['config-reset'];
            $phrase = psu_confirm_phrase('config_reset');
            break;
        case 'config_apply':
            $pairs = [];
            foreach (array_keys(psu_schema()) as $key) {
                if (isset($post[$key])) {
                    if (!is_string($post[$key])) {
                        return $reply(400, ['ok' => false, 'error' => 'params']);
                    }
                    $pairs[] = $key . '=' . $post[$key];
                }
            }
            if ($pairs === []) {
                return $reply(400, ['ok' => false, 'error' => 'params']);
            }
            $args = ['config-set', ...$pairs];
            break;
        default:
            return $reply(400, ['ok' => false, 'error' => 'params']);
    }
    if (!psu_ctl_args_ok($args)) {
        return $reply(400, ['ok' => false, 'error' => 'params']);
    }
    if ($phrase !== '' && !hash_equals($phrase, psu_str($post['confirm'] ?? ''))) {
        return $reply(400, ['ok' => false, 'error' => 'confirm', 'expected_hint' => $phrase]);
    }

    // Early, friendlier refusal; the controller enforces the gate independently.
    $status = psu_json_from(psu_run_ctl(['status', '--json'], 20));
    if ($status === null || ($status['release_gate'] ?? 'closed') === 'closed') {
        return $reply(423, ['ok' => false, 'error' => 'locked',
                            'detail' => 'The release gate is closed: changes are disabled in this WIP build.']);
    }

    // The WebUI starts a session only where the build says an admin's click is the approval; elsewhere it is the console's.
    if ($action === 'play_start' && ($status['approval_mode'] ?? 'signed') !== 'owner') {
        return $reply(423, ['ok' => false, 'error' => 'locked',
                            'detail' => 'This build needs a signed approval: start the session on the console.']);
    }
    // Recovery writes Incus' clean-stop record and puts the host back: an admin's click is its approval in an owner build only.
    if ($action === 'play_recover' && ($status['approval_mode'] ?? 'signed') !== 'owner') {
        return $reply(423, ['ok' => false, 'error' => 'locked',
                            'detail' => 'This build needs the operator on the console to recover a session.']);
    }

    // play-start verifies and stages hundreds of MB from the flash drive before it returns; play-recover reads the process
    // table and every instance's backup.yaml first.
    $slow = in_array($action, ['service', 'activate', 'rollback', 'recover', 'play_start', 'play_recover'], true);
    $result = psu_run_ctl($args, $slow ? 240 : 30);
    if ($result['timeout']) {
        return $reply(504, ['ok' => false, 'error' => 'timeout']);
    }
    if ($result['code'] !== 0) {
        return $reply(409, ['ok' => false, 'error' => 'failed', 'exit' => $result['code'], 'detail' => psu_detail($result['err'])]);
    }
    return $reply(200, ['ok' => true, 'detail' => psu_detail($result['err'])]);
}

/* ------------------------------------------------------------ view helpers ---- */

/** Wrapper classes: server-resolved theme plus the shared scope class. */
function psu_root_classes(array $display, string $extra = ''): string
{
    $theme = psu_theme($display);
    return trim('psu psu-theme-' . $theme['name'] . ($theme['light'] ? ' psu-light' : '') . ' ' . $extra);
}

/** Loads the shared script once, then initialises one view. Config is hex-escaped for inline use. */
function psu_boot_script(string $targetId, string $initFunction, array $config): string
{
    if (!in_array($initFunction, ['initTile', 'initTool', 'initSettings'], true)) {
        throw new InvalidArgumentException('unknown init function');
    }
    $src = psu_json_inline(psu_asset('js/psu.js'));
    $cfg = psu_json_inline($config);
    $id = psu_json_inline($targetId);
    return "<script>(function(){var cfg={$cfg},id={$id};function go(){var el=document.getElementById(id);"
        . "if(el&&window.PSU){window.PSU.{$initFunction}(el,cfg);}}"
        . "if(window.PSU){go();return;}var s=document.createElement('script');s.src={$src};s.onload=go;"
        . "document.head.appendChild(s);})();</script>";
}

const PSU_TAB_LABELS = ['general' => 'General', 'storage' => 'Storage & isolation', 'play' => 'Play', 'advanced' => 'Advanced'];

/** One settings row from a config-get entry. Everything dynamic is escaped. */
function psu_render_field(array $entry): string
{
    $key = (string)$entry['key'];
    $value = (string)($entry['value'] ?? '');
    $id = 'psu-f-' . strtolower($key);
    $label = psu_e($entry['label'] ?? $key);
    $attrs = ' class="psu-input" id="' . psu_e($id) . '" name="' . psu_e($key) . '" data-key="' . psu_e($key) . '"';
    if (($entry['type'] ?? '') === 'enum') {
        $control = '<select' . $attrs . '>';
        foreach ((array)($entry['values'] ?? []) as $option) {
            $control .= '<option value="' . psu_e($option) . '"' . ($option === $value ? ' selected' : '') . '>' . psu_e($option) . '</option>';
        }
        $control .= '</select>';
    } elseif (($entry['type'] ?? '') === 'int') {
        $control = '<input type="text" inputmode="numeric" autocomplete="off"' . $attrs . ' value="' . psu_e($value) . '"'
            . ' data-min="' . psu_e($entry['min'] ?? '') . '" data-max="' . psu_e($entry['max'] ?? '') . '">';
    } else {
        $control = '<input type="text" autocomplete="off" spellcheck="false"' . $attrs . ' value="' . psu_e($value) . '"'
            . ' placeholder="' . psu_e((string)($entry['default'] ?? '') !== '' ? $entry['default']
                : (($entry['type'] ?? '') === 'string' ? 'e.g. 0-15 or seat1=0-7;seat2=8-15' : '/mnt/pool/polyseat')) . '">';
    }
    $default = $entry['default'] === '' ? '(none)' : (string)$entry['default'];
    $help = $label . '<br><span class="psu-muted">Default: ' . psu_e($default)
        . (!empty($entry['locked_while_running']) ? ' &middot; locked while the service runs' : '') . '</span>';
    return '<div class="psu-field"><label for="' . psu_e($id) . '">' . psu_e((string)($entry['title'] ?? $key)) . '</label>'
        . '<div>' . $control . '<div class="psu-help">' . $help . '</div></div></div>';
}

/** @param array<int,array> $entries from config-get */
function psu_render_settings_tabs(array $entries): string
{
    $byTab = [];
    foreach ($entries as $entry) {
        $byTab[(string)($entry['tab'] ?? 'advanced')][] = $entry;
    }
    $tabs = $panels = '';
    $first = true;
    foreach (PSU_TAB_LABELS as $tab => $title) {
        if (empty($byTab[$tab])) {
            continue;
        }
        $tabs .= '<button type="button" role="tab" class="psu-tab" id="psu-tab-' . $tab . '" data-tab="' . $tab . '"'
            . ' aria-controls="psu-panel-' . $tab . '" aria-selected="' . ($first ? 'true' : 'false') . '">' . psu_e($title) . '</button>';
        $panels .= '<div role="tabpanel" class="psu-panel psu-card" id="psu-panel-' . $tab . '" aria-labelledby="psu-tab-' . $tab . '"'
            . ($first ? '' : ' hidden') . '>';
        foreach ($byTab[$tab] as $entry) {
            $panels .= psu_render_field($entry);
        }
        $panels .= '</div>';
        $first = false;
    }
    return '<div class="psu-tabs" role="tablist" aria-label="Settings">' . $tabs . '</div>' . $panels;
}
