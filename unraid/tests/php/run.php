<?php
declare(strict_types=1);
/* Tests for include/lib.php. Run: php unraid/tests/php/run.php (needs PSU_TEST_DIR env). */

$dir = (string)getenv('PSU_TEST_DIR');
define('PSU_CTL', $dir . '/stubctl');
define('PSU_SCHEMA', __DIR__ . '/../../plugin/src/emhttp/schema.json');
define('PSU_VAR_INI', $dir . '/var.ini');
define('PSU_ASSET_DIR', __DIR__ . '/../../plugin/src/emhttp');
require __DIR__ . '/../../plugin/src/emhttp/include/lib.php';

$failures = 0;
function t(string $name, callable $fn): void
{
    global $failures;
    try {
        $fn();
        echo "ok $name\n";
    } catch (Throwable $e) {
        $failures++;
        echo "FAIL $name: " . $e->getMessage() . "\n";
    }
}
function eq($a, $b, string $msg = ''): void
{
    if ($a !== $b) {
        throw new RuntimeException($msg . ' expected ' . var_export($b, true) . ' got ' . var_export($a, true));
    }
}
function ok($cond, string $msg = ''): void { if (!$cond) { throw new RuntimeException($msg ?: 'assertion failed'); } }

function stub_calls(): array
{
    $file = getenv('PSU_TEST_DIR') . '/calls.log';
    return is_file($file) ? array_map(fn($l) => explode("\x1f", $l), array_filter(explode("\n", (string)file_get_contents($file)))) : [];
}
function stub_reset(array $conf): void
{
    $d = getenv('PSU_TEST_DIR');
    @unlink("$d/calls.log");
    if (isset($conf['stderr'])) { $conf['stderr'] = base64_encode($conf['stderr']); }
    file_put_contents("$d/stub.conf", implode("\n", array_map(fn($k, $v) => "$k=$v", array_keys($conf), $conf)) . "\n");
}
const GOOD_SERVER = ['REQUEST_METHOD' => 'POST', 'HTTP_HOST' => 'tower.local', 'HTTP_ORIGIN' => 'https://tower.local'];
$SHA = str_repeat('ab', 32);
function post(array $fields, array $server = []): array
{
    $fields += ['psu_csrf' => 'TOKEN1234567890'];
    [$status, , $body] = psu_handle($server + GOOD_SERVER, [], $fields);
    return [$status, json_decode($body, true)];
}

t('validate_setting accepts and rejects like the schema', function () {
    $good = [['SERVICE', 'enabled'], ['SERVICE', 'disabled'], ['ACCEPT_NO_LSM', 'yes'], ['HEALTH_TIMEOUT', '10'],
             ['HEALTH_TIMEOUT', '300'], ['SUBID_START', '100000'], ['SUBID_COUNT', '65536'],
             ['DATA_DIR', ''], ['DATA_DIR', '/mnt/cache/appdata/polyseat'], ['DATA_DIR', '/mnt/a/.hidden'], ['DATA_DIR', '/mnt/a/b..c'], ['DATA_DIR', '/mnt/disk1/a.b-c_d'],
             ['PLAY_CPU_PERCENT', '0'], ['PLAY_CPU_PERCENT', '100'], ['PLAY_CPU_PERCENT', '007'], ['PLAY_MEMORY_MB', '0'], ['PLAY_MEMORY_MB', '4194304'],
             ['PLAY_IDLE_STOP_MINUTES', '0'], ['PLAY_IDLE_STOP_MINUTES', '1440'], ['PLAY_IDLE_STOP_MINUTES', '030'],
             ['LIBRARY_PATH', ''], ['LIBRARY_PATH', '/mnt/download/polyseat-games'], ['LIBRARY_PATH', '/mnt/disk1/a.b-c_d']];
    $bad = [['SERVICE', 'maybe'], ['SERVICE', ''], ['ACCEPT_NO_LSM', 'YES'], ['HEALTH_TIMEOUT', '9'], ['HEALTH_TIMEOUT', '301'],
            ['HEALTH_TIMEOUT', '1e2'], ['HEALTH_TIMEOUT', '-10'], ['HEALTH_TIMEOUT', ' 30'], ['HEALTH_TIMEOUT', "30\n"],
            ['SUBID_COUNT', '1'], ['DATA_DIR', '/etc'], ['DATA_DIR', '/mnt/'], ['DATA_DIR', '/mnt/../etc'], ['DATA_DIR', '/mnt/a/../b'], ['DATA_DIR', '/mnt/a/..'], ['DATA_DIR', '/mnt/./a'], ['DATA_DIR', '/mnt/a/'], ['DATA_DIR', '/mnt//a'],
            ['DATA_DIR', "/mnt/a\n"], ['DATA_DIR', '/mnt/a b'], ['DATA_DIR', '/mnt/a"b'], ['NOPE', '1'], ['', '1'],
            ['PLAY_CPU_PERCENT', '101'], ['PLAY_CPU_PERCENT', '-1'], ['PLAY_CPU_PERCENT', ''], ['PLAY_CPU_PERCENT', '5.5'], ['PLAY_CPU_PERCENT', '1e1'],
            ['PLAY_CPU_PERCENT', ' 5'], ['PLAY_CPU_PERCENT', "5\n"], ['PLAY_MEMORY_MB', '4194305'], ['PLAY_MEMORY_MB', '8G'], ['PLAY_MEMORY_MB', '99999999999'],
            ['PLAY_IDLE_STOP_MINUTES', '1441'], ['PLAY_IDLE_STOP_MINUTES', ''], ['PLAY_IDLE_STOP_MINUTES', '5.5'], ['PLAY_IDLE_STOP_MINUTES', "30\n"],
            ['LIBRARY_PATH', '/etc'], ['LIBRARY_PATH', '/mnt/'], ['LIBRARY_PATH', '/mnt/a/../b'], ['LIBRARY_PATH', '/mnt/a/'], ['LIBRARY_PATH', "/mnt/a\n"],
            ['LIBRARY_PATH', '/mnt/a b'], ['PLAY_PIDS_MAX', '1']];
    foreach ($good as [$k, $v]) { ok(psu_validate_setting($k, $v), "$k=$v should pass"); }
    foreach ($bad as [$k, $v]) { ok(!psu_validate_setting($k, $v), "$k=" . json_encode($v) . ' should fail'); }
});

t('csrf is fail-closed', function () {
    ok(psu_csrf_ok('abcdef123456', 'abcdef123456'));
    foreach ([[null, 'abcdef123456'], ['abcdef123456', null], ['', ''], [null, null], ['abcdef123456', ''], ['x', 'abcdef123456']] as [$s, $e]) {
        ok(!psu_csrf_ok($s, $e), json_encode([$s, $e]));
    }
    file_put_contents(PSU_VAR_INI, "NAME=\"Tower\"\ncsrf_token=\"0123456789ABCDEF\"\nOTHER=1\n");
    eq(psu_csrf_expected(), '0123456789ABCDEF');
    file_put_contents(PSU_VAR_INI, "csrf_token=\"short\"\n");
    eq(psu_csrf_expected(), null);
    file_put_contents(PSU_VAR_INI, "nothing here\n");
    eq(psu_csrf_expected(), null);
    unlink(PSU_VAR_INI);
    eq(psu_csrf_expected(), null);
    file_put_contents(PSU_VAR_INI, "csrf_token=\"TOKEN1234567890\"\n");
});

t('origin check is same-origin and fail-closed', function () {
    $h = 'tower.local';
    ok(psu_origin_ok(['HTTP_HOST' => $h, 'HTTP_ORIGIN' => 'https://tower.local']));
    ok(psu_origin_ok(['HTTP_HOST' => 'tower:8443', 'HTTP_ORIGIN' => 'https://tower:8443']));
    ok(psu_origin_ok(['HTTP_HOST' => $h, 'HTTP_REFERER' => 'https://tower.local/Tools/PolyseatTool']));
    ok(!psu_origin_ok(['HTTP_HOST' => $h]), 'no origin/referer');
    ok(!psu_origin_ok(['HTTP_HOST' => $h, 'HTTP_ORIGIN' => 'https://evil.example']));
    ok(!psu_origin_ok(['HTTP_HOST' => $h, 'HTTP_ORIGIN' => 'https://tower.local.evil.example']));
    ok(!psu_origin_ok(['HTTP_HOST' => $h, 'HTTP_ORIGIN' => 'https://tower.local@evil.example']));
    ok(!psu_origin_ok(['HTTP_HOST' => 'tower:8443', 'HTTP_ORIGIN' => 'https://tower']));
    ok(!psu_origin_ok(['HTTP_HOST' => $h, 'HTTP_ORIGIN' => 'null']));
    ok(!psu_origin_ok(['HTTP_HOST' => '', 'HTTP_ORIGIN' => 'https://tower.local']));
    ok(!psu_origin_ok(['HTTP_HOST' => $h, 'HTTP_ORIGIN' => 'https://evil.example', 'HTTP_REFERER' => 'https://tower.local/']), 'origin wins over referer');
});

t('argument allowlist', function () use ($SHA) {
    $good = [['status'], ['status', '--json'], ['metrics'], ['list'], ['config-get'], ['logs', 'controller', '100'],
             ['logs', 'incusd', '500'], ['diagnose'], ['verify', $SHA], ['start'], ['stop'], ['restart'],
             ['activate', $SHA, '--approve'], ['rollback', '--approve'], ['recover', '--approve'], ['config-reset'],
             ['config-set', 'SERVICE=enabled'], ['config-set', 'SERVICE=enabled', 'HEALTH_TIMEOUT=30'], ['polyseatd-status'],
             ['config-set', 'PLAY_CPU_PERCENT=50', 'PLAY_MEMORY_MB=8192', 'LIBRARY_PATH=/mnt/tank/games']];
    $bad = [[], ['uninstall', '--approve'], ['update', 'a', 'b', 'c', '--approve'], ['stage', 'a', 'b', 'c', '--approve'],
            ['boot'], ['status', '--json', 'x'], ['status', '-x'], ['activate', $SHA], ['activate', $SHA, '--approve', 'x'],
            ['activate', 'abc', '--approve'], ['rollback'], ['recover'], ['logs', 'controller', '0'], ['logs', 'controller', '501'],
            ['logs', '../etc/passwd', '10'], ['logs', 'controller', '10', 'extra'], ['verify', 'abc'],
            ['config-set'], ['config-set', 'UNKNOWN=1'], ['config-set', 'SERVICE=maybe'], ['config-set', "SERVICE=enabled\nDATA_DIR=/mnt/x"],
            ['config-set', 'DATA_DIR=/mnt/a;rm -rf /'], ['status', "a\0b"], ['status', str_repeat('a', 301)],
            ['start', '--now'], ['stop', ''], ['x' => 'status'], ['status', 5], ['STATUS'], ['status '],
            ['polyseatd-status', 'x'], ['polyseatd-status', '--json'], ['polyseatd-stop'], ['polyseatd-play'], ['polyseatd-session'],
            ['config-set', 'PLAY_CPU_PERCENT=101'], ['config-set', 'LIBRARY_PATH=/etc'], ['config-set', 'PLAY_MEMORY_MB=8G']];
    foreach ($good as $args) { ok(psu_ctl_args_ok($args), json_encode($args) . ' should pass'); }
    foreach ($bad as $args) { ok(!psu_ctl_args_ok($args), json_encode($args) . ' should fail'); }
});

t('psu_run_ctl refuses rejected vectors without spawning anything', function () {
    stub_reset(['gate' => 'open']);
    $r = psu_run_ctl(['uninstall', '--approve']);
    eq($r['code'], 1);
    eq(stub_calls(), []);
});

t('psu_run_ctl enforces its timeout', function () {
    stub_reset(['gate' => 'open', 'sleep' => 5]);
    $start = microtime(true);
    $r = psu_run_ctl(['metrics'], 1);
    ok($r['timeout'] && $r['code'] === 124, 'timeout flagged');
    ok(microtime(true) - $start < 4, 'returned promptly');
});

t('GET views are read-only and validated', function () {
    stub_reset(['gate' => 'closed']);
    foreach (['status', 'metrics', 'list', 'config'] as $action) {
        [$code, , $body] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => $action], []);
        eq($code, 200, $action);
        eq(json_decode($body, true)['ok'], true);
    }
    [$code] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => 'logs', 'which' => 'controller', 'lines' => '501'], []);
    eq($code, 400);
    [$code] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => 'logs', 'which' => '../x', 'lines' => '5'], []);
    eq($code, 400);
    [$code] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => 'activate', 'sha' => 'x'], []);
    eq($code, 400, 'mutating actions are not reachable by GET');
    foreach (stub_calls() as $call) {
        ok(in_array($call[0], ['status', 'metrics', 'list', 'config-get', 'logs', 'polyseatd-status'], true), 'only read commands: ' . $call[0]);
    }
    [$code, $headers] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => 'status'], []);
    eq($headers['Cache-Control'], 'no-store');
    eq($headers['X-Content-Type-Options'], 'nosniff');
});

t('the play view is read-only, reduced to validated fields and tells "no session" from "unavailable"', function () {
    $get = fn() => psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => 'play'], []);
    stub_reset(['gate' => 'closed', 'play' => 'up']);
    [$code, , $body] = $get();
    $b = json_decode($body, true);
    eq($code, 200);
    eq($b['data'], ['url' => 'https://192.168.178.24:47800', 'alive' => true,
                    'seats' => [['name' => 'seat1', 'status' => 'Running'], ['name' => 'seat2', 'status' => 'Stopped']],
                    'expires_at' => 2000000000], 'only the validated fields; the bad seat name and the session blob are dropped');
    ok(is_int($b['now']), 'server time for the remaining-time display');
    stub_reset(['gate' => 'closed', 'play' => 'evil']);
    [, , $body] = $get();
    eq(json_decode($body, true)['data'], ['url' => '', 'alive' => true, 'seats' => null, 'expires_at' => null]);
    stub_reset(['gate' => 'closed', 'play' => 'none']);
    [$code, , $body] = $get();
    eq([$code, json_decode($body, true)['data']], [200, null], 'no session is an answer');
    stub_reset(['gate' => 'closed', 'play' => 'broken']);
    [$code, , $body] = $get();
    eq([$code, json_decode($body, true)['error']], [502, 'unavailable']);
    foreach (stub_calls() as $call) { eq($call, ['polyseatd-status']); }
    // The play verbs stay out of reach: not by GET, not by POST, not through the argument allowlist.
    stub_reset(['gate' => 'open']);
    foreach (['polyseatd_play', 'polyseatd-play', 'play_start', 'play_stop', 'polyseatd-stop'] as $action) {
        [$code] = post(['action' => $action]);
        eq($code, 400, $action);
        [$code] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => $action], []);
        eq($code, 400, "GET $action");
    }
    eq(stub_calls(), []);
});

t('POST rejects bad method, origin and csrf before anything else', function () use ($SHA) {
    stub_reset(['gate' => 'open']);
    eq(psu_handle(['REQUEST_METHOD' => 'PUT'], [], [])[0], 405);
    eq(psu_handle(['REQUEST_METHOD' => 'DELETE'], [], [])[0], 405);
    [$code, $b] = post(['action' => 'service', 'do' => 'start'], ['HTTP_ORIGIN' => 'https://evil.example']);
    eq([$code, $b['error']], [403, 'origin']);
    [$code, $b] = post(['action' => 'service', 'do' => 'start'], ['HTTP_ORIGIN' => '', 'HTTP_REFERER' => '']);
    eq([$code, $b['error']], [403, 'origin']);
    [$code, $b] = post(['action' => 'service', 'do' => 'start', 'psu_csrf' => 'WRONGTOKEN123456']);
    eq([$code, $b['error']], [403, 'csrf']);
    [, , $body] = psu_handle(GOOD_SERVER, [], ['action' => 'service', 'do' => 'start']);
    eq(json_decode($body, true)['error'], 'csrf', 'missing token');
    [, , $body] = psu_handle(GOOD_SERVER, [], ['action' => 'service', 'do' => 'start', 'psu_csrf' => ['x']]);
    eq(json_decode($body, true)['error'], 'csrf', 'array token');
    unlink(PSU_VAR_INI);
    [$code, $b] = post(['action' => 'service', 'do' => 'start']);
    eq([$code, $b['error']], [403, 'csrf'], 'no server-side token at all');
    file_put_contents(PSU_VAR_INI, "csrf_token=\"TOKEN1234567890\"\n");
    eq(stub_calls(), [], 'the controller was never called');
});

t('destructive actions need the exact typed phrase', function () use ($SHA) {
    stub_reset(['gate' => 'open']);
    foreach ([['service', ['do' => 'stop'], 'STOP'], ['service', ['do' => 'restart'], 'RESTART'],
              ['rollback', [], 'ROLLBACK'], ['recover', [], 'RECOVER'], ['config_reset', [], 'RESET'],
              ['activate', ['sha' => $SHA], 'ACTIVATE abababab']] as [$action, $extra, $phrase]) {
        [$code, $b] = post(['action' => $action] + $extra);
        eq([$code, $b['error']], [400, 'confirm'], "$action without phrase");
        [$code, $b] = post(['action' => $action, 'confirm' => strtolower($phrase)] + $extra);
        eq($code, 400, "$action lower-case phrase");
        [$code, $b] = post(['action' => $action, 'confirm' => $phrase . ' '] + $extra);
        eq($code, 400, "$action padded phrase");
    }
    foreach (stub_calls() as $call) { ok($call[0] === 'status', 'no mutation before confirmation: ' . $call[0]); }
});

t('the release gate blocks every mutation in the UI path too', function () use ($SHA) {
    stub_reset(['gate' => 'closed']);
    foreach ([['service', ['do' => 'start']], ['config_apply', ['SERVICE' => 'enabled']],
              ['rollback', ['confirm' => 'ROLLBACK']], ['service', ['do' => 'stop', 'confirm' => 'STOP']]] as [$action, $extra]) {
        [$code, $b] = post(['action' => $action] + $extra);
        eq([$code, $b['error']], [423, 'locked'], $action);
    }
    foreach (stub_calls() as $call) { eq($call[0], 'status', 'only the status probe ran'); }
    stub_reset(['gate' => 'weird']);
    [$code] = post(['action' => 'service', 'do' => 'start']);
    eq($code, 200, 'any non-closed gate value is passed on to the controller, which enforces its own gate');
});

t('allowed actions reach the controller with exactly the validated vector', function () use ($SHA) {
    stub_reset(['gate' => 'open']);
    [$code] = post(['action' => 'service', 'do' => 'start']); eq($code, 200);
    [$code] = post(['action' => 'service', 'do' => 'stop', 'confirm' => 'STOP']); eq($code, 200);
    [$code] = post(['action' => 'activate', 'sha' => $SHA, 'confirm' => 'ACTIVATE abababab']); eq($code, 200);
    [$code] = post(['action' => 'rollback', 'confirm' => 'ROLLBACK']); eq($code, 200);
    [$code] = post(['action' => 'config_apply', 'SERVICE' => 'enabled', 'HEALTH_TIMEOUT' => '45', 'EVIL' => 'x', 'psu_csrf2' => 'y']); eq($code, 200);
    [$code] = post(['action' => 'config_apply', 'PLAY_CPU_PERCENT' => '50', 'PLAY_MEMORY_MB' => '0', 'LIBRARY_PATH' => '/mnt/tank/games']); eq($code, 200);
    $mutations = array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status'));
    eq($mutations, [['start'], ['stop'], ['activate', $SHA, '--approve'], ['rollback', '--approve'],
                    ['config-set', 'SERVICE=enabled', 'HEALTH_TIMEOUT=45'],
                    ['config-set', 'PLAY_CPU_PERCENT=50', 'PLAY_MEMORY_MB=0', 'LIBRARY_PATH=/mnt/tank/games']]);
});

t('bad parameters never reach the controller', function () use ($SHA) {
    stub_reset(['gate' => 'open']);
    foreach ([['service', ['do' => 'uninstall']], ['service', ['do' => 'start --approve']], ['service', []],
              ['activate', ['sha' => 'zz', 'confirm' => 'ACTIVATE zz']], ['activate', ['sha' => strtoupper($SHA)]],
              ['config_apply', ['SERVICE' => 'nope']], ['config_apply', ['DATA_DIR' => '/etc']], ['config_apply', []],
              ['config_apply', ['SERVICE' => ['enabled']]], ['config_apply', ['PLAY_CPU_PERCENT' => '101']],
              ['config_apply', ['LIBRARY_PATH' => '/mnt/a/../b']], ['config_apply', ['PLAY_MEMORY_MB' => '-1']], ['nonsense', []], ['', []], ['update', ['archive' => '/x']],
              ['uninstall', ['confirm' => 'UNINSTALL']]] as [$action, $extra]) {
        [$code] = post(['action' => $action] + $extra);
        ok($code === 400, "$action should be 400, got $code");
    }
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), []);
});

t('controller failures map to generic, bounded messages', function () use ($SHA) {
    stub_reset(['gate' => 'open', 'fail' => 5, 'stderr' => "2026 [error] bundle failed \x1b[31mred\x1b[0m " . str_repeat('x', 500)]);
    [$code, $b] = post(['action' => 'service', 'do' => 'start']);
    eq([$code, $b['error'], $b['exit']], [409, 'failed', 5]);
    ok(strlen($b['detail']) <= 300 && strpos($b['detail'], "\x1b") === false, 'detail bounded and printable');
});

t('rendering escapes hostile data', function () {
    $evil = '"><script>alert(1)</script>';
    $html = psu_render_field(['key' => 'DATA_DIR', 'type' => 'path', 'label' => $evil, 'value' => $evil, 'default' => $evil,
                              'tab' => 'storage', 'locked_while_running' => true]);
    ok(strpos($html, '<script') === false, 'no raw script tag');
    ok(strpos($html, '&lt;script&gt;') !== false);
    $html = psu_render_field(['key' => 'SERVICE', 'type' => 'enum', 'values' => ['a', $evil], 'value' => $evil, 'label' => 'x', 'default' => 'a']);
    ok(strpos($html, '<script') === false);
    $tabs = psu_render_settings_tabs([['key' => 'SERVICE', 'type' => 'enum', 'values' => ['enabled', 'disabled'], 'value' => 'enabled',
                                       'label' => 'L', 'default' => 'disabled', 'tab' => 'general']]);
    ok(strpos($tabs, 'role="tablist"') !== false && strpos($tabs, 'role="tabpanel"') !== false);
    $play = psu_render_settings_tabs([['key' => 'PLAY_CPU_PERCENT', 'type' => 'int', 'min' => 0, 'max' => 100, 'value' => '0', 'title' => 'T',
                                       'label' => 'L', 'default' => '0', 'tab' => 'play']]);
    ok(strpos($play, 'data-tab="play"') !== false && strpos($play, '>Play</button>') !== false && strpos($play, 'name="PLAY_CPU_PERCENT"') !== false);
    $inline = psu_json_inline(['x' => '</script><img src=x onerror=alert(1)>', 'y' => "a'b\"c&d"]);
    ok(strpos($inline, '</script>') === false && strpos($inline, '<') === false && strpos($inline, "'") === false);
    eq(json_decode($inline, true)['x'], '</script><img src=x onerror=alert(1)>', 'round-trips');
});

t('theme and boot script helpers are whitelisted', function () {
    eq(psu_theme(['theme' => 'azure']), ['name' => 'azure', 'light' => true]);
    eq(psu_theme(['theme' => 'black']), ['name' => 'black', 'light' => false]);
    eq(psu_theme(['theme' => '"><x']), ['name' => 'black', 'light' => false]);
    eq(psu_theme([]), ['name' => 'black', 'light' => false]);
    ok(strpos(psu_root_classes(['theme' => 'white'], 'psu-tile'), 'psu-light') !== false);
    $threw = false;
    try { psu_boot_script('x', 'alert(1)//', []); } catch (InvalidArgumentException $e) { $threw = true; }
    ok($threw, 'unknown init function rejected');
    $script = psu_boot_script('psu-tool', 'initTool', ['csrf' => '</script>']);
    ok(substr_count($script, '</script>') === 1, 'only the closing tag of the wrapper');
});

exit($failures === 0 ? 0 : 1);
