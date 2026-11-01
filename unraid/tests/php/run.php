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
             ['LIBRARY_PATH', ''], ['LIBRARY_PATH', '/mnt/download/polyseat-games'], ['LIBRARY_PATH', '/mnt/disk1/a.b-c_d'],
             ['PLAY_SESSION_HOURS', '1'], ['PLAY_SESSION_HOURS', '6'], ['PLAY_SESSION_HOURS', '24'], ['PLAY_SESSION_HOURS', '06'],
             ['PLAY_CPU_PERFORMANCE', 'yes'], ['PLAY_CPU_PERFORMANCE', 'no'],
             ['PLAY_SEAT_CPUS', ''], ['PLAY_SEAT_CPUS', '0-15'], ['PLAY_SEAT_CPUS', '0,2,4-7'], ['PLAY_SEAT_CPUS', 'seat1=0-7;seat2=8-15'],
             ['PLAY_SEAT_CPUS', 'seat-1=3'],
             ['WORKLOADS_ENABLED', 'yes'], ['WORKLOADS_ENABLED', 'no'], ['WORKLOADS_INSTANCES', ''], ['WORKLOADS_INSTANCES', 'ubuntudev'],
             ['WORKLOADS_INSTANCES', 'ubuntudev ubuntudev-test'], ['WORKLOADS_MEMORY_MB', '0'], ['WORKLOADS_MEMORY_MB', '28672'],
             ['WORKLOADS_MEMORY_MB', '4194304'], ['WORKLOADS_CPUS', ''], ['WORKLOADS_CPUS', '0-15'], ['WORKLOADS_CPUS', '0-7,16-19']];
    $bad = [['SERVICE', 'maybe'], ['SERVICE', ''], ['ACCEPT_NO_LSM', 'YES'], ['HEALTH_TIMEOUT', '9'], ['HEALTH_TIMEOUT', '301'],
            ['HEALTH_TIMEOUT', '1e2'], ['HEALTH_TIMEOUT', '-10'], ['HEALTH_TIMEOUT', ' 30'], ['HEALTH_TIMEOUT', "30\n"],
            ['SUBID_COUNT', '1'], ['DATA_DIR', '/etc'], ['DATA_DIR', '/mnt/'], ['DATA_DIR', '/mnt/../etc'], ['DATA_DIR', '/mnt/a/../b'], ['DATA_DIR', '/mnt/a/..'], ['DATA_DIR', '/mnt/./a'], ['DATA_DIR', '/mnt/a/'], ['DATA_DIR', '/mnt//a'],
            ['DATA_DIR', "/mnt/a\n"], ['DATA_DIR', '/mnt/a b'], ['DATA_DIR', '/mnt/a"b'], ['NOPE', '1'], ['', '1'],
            ['PLAY_CPU_PERCENT', '101'], ['PLAY_CPU_PERCENT', '-1'], ['PLAY_CPU_PERCENT', ''], ['PLAY_CPU_PERCENT', '5.5'], ['PLAY_CPU_PERCENT', '1e1'],
            ['PLAY_CPU_PERCENT', ' 5'], ['PLAY_CPU_PERCENT', "5\n"], ['PLAY_MEMORY_MB', '4194305'], ['PLAY_MEMORY_MB', '8G'], ['PLAY_MEMORY_MB', '99999999999'],
            ['PLAY_IDLE_STOP_MINUTES', '1441'], ['PLAY_IDLE_STOP_MINUTES', ''], ['PLAY_IDLE_STOP_MINUTES', '5.5'], ['PLAY_IDLE_STOP_MINUTES', "30\n"],
            ['LIBRARY_PATH', '/etc'], ['LIBRARY_PATH', '/mnt/'], ['LIBRARY_PATH', '/mnt/a/../b'], ['LIBRARY_PATH', '/mnt/a/'], ['LIBRARY_PATH', "/mnt/a\n"],
            ['LIBRARY_PATH', '/mnt/a b'], ['PLAY_PIDS_MAX', '1'],
            ['PLAY_SESSION_HOURS', '0'], ['PLAY_SESSION_HOURS', '25'], ['PLAY_SESSION_HOURS', ''], ['PLAY_SESSION_HOURS', '1.5'], ['PLAY_SESSION_HOURS', "6\n"],
            ['PLAY_CPU_PERFORMANCE', 'true'], ['PLAY_CPU_PERFORMANCE', 'YES'], ['PLAY_CPU_PERFORMANCE', ''],
            ['PLAY_SEAT_CPUS', '0-'], ['PLAY_SEAT_CPUS', '-3'], ['PLAY_SEAT_CPUS', '0 -3'], ['PLAY_SEAT_CPUS', 'seat1='], ['PLAY_SEAT_CPUS', 'Seat1=0-7'],
            ['PLAY_SEAT_CPUS', 'seat1=0-7;'], ['PLAY_SEAT_CPUS', 'seat1=0-7;seat2'], ['PLAY_SEAT_CPUS', '0-7;seat1=1'], ['PLAY_SEAT_CPUS', 'seat1=0-7,'],
            ['PLAY_SEAT_CPUS', "0-15\n"], ['PLAY_SEAT_CPUS', '12345'], ['PLAY_SEAT_CPUS', str_repeat('1,', 200) . '1'], ['PLAY_SEAT_CPUS', '$(id)'],
            ['WORKLOADS_ENABLED', 'true'], ['WORKLOADS_ENABLED', ''], ['WORKLOADS_INSTANCES', 'Ubuntudev'], ['WORKLOADS_INSTANCES', 'a  b'],
            ['WORKLOADS_INSTANCES', ' a'], ['WORKLOADS_INSTANCES', 'a '], ['WORKLOADS_INSTANCES', "a\nb"], ['WORKLOADS_INSTANCES', 'a;b'],
            ['WORKLOADS_INSTANCES', '-a'], ['WORKLOADS_INSTANCES', str_repeat('a ', 16) . 'a'], ['WORKLOADS_INSTANCES', str_repeat('a', 64)],
            ['WORKLOADS_MEMORY_MB', '4194305'], ['WORKLOADS_MEMORY_MB', '28G'], ['WORKLOADS_MEMORY_MB', ''], ['WORKLOADS_CPUS', '0-'],
            ['WORKLOADS_CPUS', '0-7;1'], ['WORKLOADS_CPUS', 'seat1=0-7'], ['WORKLOADS_CPUS', "0-7\n"], ['WORKLOADS_CPUS', '12345']];
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
             ['config-set', 'SERVICE=enabled'], ['config-set', 'SERVICE=enabled', 'HEALTH_TIMEOUT=30'], ['polyseatd-status'], ['play-status'], ['play-start'], ['play-stop'], ['play-recover'],
             ['config-set', 'PLAY_CPU_PERCENT=50', 'PLAY_MEMORY_MB=8192', 'LIBRARY_PATH=/mnt/tank/games'],
             ['workloads-status'], ['workloads-start'], ['workloads-stop'], ['logs', 'workloads', '200'],
             ['config-set', 'WORKLOADS_ENABLED=yes', 'WORKLOADS_INSTANCES=ubuntudev', 'WORKLOADS_MEMORY_MB=28672', 'WORKLOADS_CPUS=0-15'],
             ['config-set', 'PLAY_SESSION_HOURS=12', 'PLAY_CPU_PERFORMANCE=yes', 'PLAY_SEAT_CPUS=seat1=0-7;seat2=8-15']];
    $bad = [[], ['uninstall', '--approve'], ['update', 'a', 'b', 'c', '--approve'], ['stage', 'a', 'b', 'c', '--approve'],
            ['boot'], ['status', '--json', 'x'], ['status', '-x'], ['activate', $SHA], ['activate', $SHA, '--approve', 'x'],
            ['activate', 'abc', '--approve'], ['rollback'], ['recover'], ['logs', 'controller', '0'], ['logs', 'controller', '501'],
            ['logs', '../etc/passwd', '10'], ['logs', 'controller', '10', 'extra'], ['verify', 'abc'],
            ['config-set'], ['config-set', 'UNKNOWN=1'], ['config-set', 'SERVICE=maybe'], ['config-set', "SERVICE=enabled\nDATA_DIR=/mnt/x"],
            ['config-set', 'DATA_DIR=/mnt/a;rm -rf /'], ['status', "a\0b"], ['status', str_repeat('a', 301)],
            ['start', '--now'], ['stop', ''], ['x' => 'status'], ['status', 5], ['STATUS'], ['status '],
            ['polyseatd-status', 'x'], ['polyseatd-status', '--json'], ['polyseatd-stop'], ['polyseatd-play'], ['polyseatd-session'],
            ['config-set', 'PLAY_CPU_PERCENT=101'], ['config-set', 'LIBRARY_PATH=/etc'], ['config-set', 'PLAY_MEMORY_MB=8G'],
            ['play-start', '--approve'], ['play-start', 'x'], ['play-stop', '--wait', '300'], ['play-stop', 'x'], ['play-status', 'x'],
            ['play-recover', '--approve'], ['play-recover', 'x'], ['play-recover', '--force'], ['Play-Recover'],
            ['config-set', 'PLAY_SESSION_HOURS=25'], ['config-set', 'PLAY_SEAT_CPUS=0-15;rm'], ['config-set', 'PLAY_CPU_PERFORMANCE=true'],
            ['workloads-start', '--boot'], ['workloads-start', '--approve'], ['workloads-stop', '--wait', '600'], ['workloads-stop', 'x'],
            ['workloads-status', 'x'], ['workloads-status', '--json'], ['workloads-run'], ['workloads-run', 'run'], ['workloads-run', 'adopt'],
            ['workloads-recover'], ['workloads-recover', '--approve'], ['stop', '--emergency'], ['stop', '--wait', '300'], ['logs', 'workloads', '0'], ['logs', 'workload', '10'],
            ['config-set', 'WORKLOADS_INSTANCES=Bad Name'], ['config-set', 'WORKLOADS_CPUS=0-'], ['config-set', 'WORKLOADS_MEMORY_MB=8G']];
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
    eq($b['state'], 'running');
    stub_reset(['gate' => 'closed', 'play' => 'none']);
    [$code, , $body] = $get();
    eq([$code, json_decode($body, true)['data']], [200, null], 'no session is an answer');
    eq(json_decode($body, true)['state'], 'idle', 'a controller without a state is idle');
    stub_reset(['gate' => 'closed', 'play' => 'broken']);
    [$code, , $body] = $get();
    eq([$code, json_decode($body, true)['error']], [502, 'unavailable']);
    foreach (stub_calls() as $call) { eq($call, ['polyseatd-status']); }
    // The session's own verbs stay out of reach: not by GET, not by POST, not through the argument allowlist. Start and
    // stop of a session are the POST actions play_start/play_stop (below); never by GET, never with a body argument.
    stub_reset(['gate' => 'open']);
    foreach (['polyseatd_play', 'polyseatd-play', 'play-start', 'play-stop', 'polyseatd-stop'] as $action) {
        [$code] = post(['action' => $action]);
        eq($code, 400, $action);
    }
    foreach (['polyseatd_play', 'polyseatd-play', 'play_start', 'play_stop', 'play-start', 'play-stop', 'polyseatd-stop'] as $action) {
        [$code] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => $action], []);
        eq($code, 400, "GET $action");
    }
    eq(stub_calls(), []);
});

t('the play view carries the state, a validated reason and how the last session ended', function () {
    $get = fn() => json_decode(psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => 'play'], [])[2], true);
    stub_reset(['gate' => 'closed', 'play' => 'recover']);
    $b = $get();
    eq([$b['ok'], $b['data'], $b['state'], $b['reason'], $b['last_end']], [true, null, 'needs-recovery', 'memory-floor-hard', null]);
    stub_reset(['gate' => 'closed', 'play' => 'idle']);
    $b = $get();
    eq([$b['data'], $b['state'], $b['reason']], [null, 'idle', null]);
    eq($b['last_end'], ['reason' => 'memory-floor', 'clean' => true, 'recovered' => false, 'ended_at' => 1700000000], 'only the validated fields');
    stub_reset(['gate' => 'closed', 'play' => 'busy']);
    $b = $get();
    eq([$b['data'], $b['state']], [null, 'busy']);
    stub_reset(['gate' => 'closed', 'play' => 'recover-evil']);
    $b = $get();
    eq([$b['state'], $b['reason'], $b['last_end']], ['needs-recovery', 'unknown', null], 'a hostile reason becomes "unknown", a hostile last_end is dropped');
    stub_reset(['gate' => 'closed', 'play' => 'bogus-state']);
    [$code] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => 'play'], []);
    eq($code, 502, 'a state nobody defined is not an answer');
    foreach (stub_calls() as $call) { eq($call, ['polyseatd-status']); }
});

t('play recovery: only in an owner build, only with the typed phrase, nothing else becomes an argument', function () {
    stub_reset(['gate' => 'canary', 'mode' => 'owner']);
    [$code, $b] = post(['action' => 'play_recover']);
    eq([$code, $b['error'], $b['expected_hint']], [400, 'confirm', 'WIEDERHERSTELLEN']);
    foreach (['wiederherstellen', 'RECOVER', 'STOP', 'WIEDERHERSTELLEN '] as $typed) {
        [$code, $b] = post(['action' => 'play_recover', 'confirm' => $typed]);
        eq([$code, $b['error']], [400, 'confirm'], $typed);
    }
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), [], 'nothing ran before the phrase matched');
    [$code] = post(['action' => 'play_recover', 'confirm' => 'WIEDERHERSTELLEN', 'wait' => '1', 'approve' => '1', 'args' => ['--force']]); eq($code, 200);
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), [['play-recover']]);
    // a signed build, a missing mode and a closed gate: refused before the controller is asked
    foreach ([['gate' => 'canary', 'mode' => 'signed'], ['gate' => 'canary'], ['gate' => 'closed', 'mode' => 'owner']] as $conf) {
        stub_reset($conf);
        [$code, $b] = post(['action' => 'play_recover', 'confirm' => 'WIEDERHERSTELLEN']);
        eq([$code, $b['error']], [423, 'locked'], json_encode($conf));
        eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), []);
    }
    // a refusal of the controller reaches the card with its last line
    stub_reset(['gate' => 'canary', 'mode' => 'owner', 'fail' => 2,
                'stderr' => "[error] play-recover: incusd is still running (pid 4242)\n[error] play-recover: refused, nothing was changed: incusd is still running (pid 4242)"]);
    [$code, $b] = post(['action' => 'play_recover', 'confirm' => 'WIEDERHERSTELLEN']);
    eq([$code, $b['error'], $b['exit']], [409, 'failed', 2]);
    ok(strpos($b['detail'], 'refused, nothing was changed: incusd is still running') !== false, $b['detail']);
    // never by GET, never as a controller verb of its own
    foreach (['play_recover', 'play-recover'] as $action) {
        [$code] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => $action], []);
        eq($code, 400, "GET $action");
    }
    [$code] = post(['action' => 'play-recover', 'confirm' => 'WIEDERHERSTELLEN']);
    eq($code, 400, 'only the underscore action exists');
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
              ['rollback', [], 'ROLLBACK'], ['recover', [], 'RECOVER'], ['config_reset', [], 'RESET'], ['play_stop', [], 'STOP'],
              ['play_recover', [], 'WIEDERHERSTELLEN'],
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

t('play start and stop: start only where an admin click is the approval, stop with the typed phrase', function () {
    stub_reset(['gate' => 'canary', 'mode' => 'owner']);
    [$code, $b] = post(['action' => 'play_stop']);
    eq([$code, $b['error']], [400, 'confirm']);
    [$code] = post(['action' => 'play_stop', 'confirm' => 'STOP']); eq($code, 200);
    [$code] = post(['action' => 'play_start']); eq($code, 200);
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), [['play-stop'], ['play-start']]);
    // a signed-approval build: the stop still works, the start is refused before the controller is asked
    stub_reset(['gate' => 'canary', 'mode' => 'signed']);
    [$code, $b] = post(['action' => 'play_start']);
    eq([$code, $b['error']], [423, 'locked']);
    ok(strpos($b['detail'], 'console') !== false);
    [$code] = post(['action' => 'play_stop', 'confirm' => 'STOP']); eq($code, 200);
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), [['play-stop']]);
    // the same with a missing mode (an older controller) and with a closed gate
    stub_reset(['gate' => 'canary']);
    [$code] = post(['action' => 'play_start']); eq($code, 423);
    stub_reset(['gate' => 'closed', 'mode' => 'owner']);
    [$code] = post(['action' => 'play_start']); eq($code, 423);
    [$code] = post(['action' => 'play_stop', 'confirm' => 'STOP']); eq($code, 423);
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), []);
    // extra fields never become arguments
    stub_reset(['gate' => 'canary', 'mode' => 'owner']);
    [$code] = post(['action' => 'play_start', 'wait' => '300', 'approve' => '1', 'args' => ['--approve']]); eq($code, 200);
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), [['play-start']]);
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

t('the workloads view is read-only, reduced to validated fields and tells "not in this build" from "unavailable"', function () {
    $get = fn() => psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => 'workloads'], []);
    stub_reset(['gate' => 'closed', 'workloads' => 'up']);
    [$code, , $body] = $get();
    $b = json_decode($body, true);
    eq($code, 200);
    eq($b['data']['state'], 'running');
    eq($b['data']['enabled'], true);
    eq($b['data']['policy_ok'], false);
    eq($b['data']['clean_stop'], false);
    eq($b['data']['instances'], [['name' => 'ubuntudev', 'status' => 'Running', 'ips' => ['192.168.178.144'], 'autostart' => true],
                                 ['name' => 'test', 'status' => 'Stopped', 'ips' => [], 'autostart' => false]],
       'only valid names and IPv4 addresses; the bad name, the IPv6 and the markup are dropped');
    eq($b['data']['violations'], ['instance ubuntudev: device usb', 'instance x: <b>bad</b>?'], 'printable ASCII only (the page sets it as text)');
    ok(is_int($b['now']));
    stub_reset(['gate' => 'closed', 'workloads' => 'evil']);
    [, , $body] = $get();
    eq(json_decode($body, true)['data'], ['state' => 'stopped', 'enabled' => false, 'policy_ok' => true, 'violations' => [],
                                          'instances' => [], 'clean_stop' => null, 'note' => '', 'updated' => null]);
    stub_reset(['gate' => 'closed', 'workloads' => 'none']);
    [$code, , $body] = $get();
    eq([$code, json_decode($body, true)['data']], [200, null], 'a build without the daemon is an answer');
    stub_reset(['gate' => 'closed', 'workloads' => 'broken']);
    [$code, , $body] = $get();
    eq([$code, json_decode($body, true)['error']], [502, 'unavailable']);
    foreach (stub_calls() as $call) { eq($call, ['workloads-status']); }
    // never by GET, and the internal and console verbs never by POST
    stub_reset(['gate' => 'open']);
    foreach (['workloads_start', 'workloads_stop', 'workloads-start', 'workloads-stop', 'workloads-run', 'workloads-recover', 'workloads_recover'] as $action) {
        [$code] = psu_handle(['REQUEST_METHOD' => 'GET'], ['action' => $action], []);
        eq($code, 400, "GET $action");
    }
    foreach (['workloads-start', 'workloads-stop', 'workloads-run', 'workloads_run', 'workloads-recover', 'workloads_recover'] as $action) {
        [$code] = post(['action' => $action]);
        eq($code, 400, "POST $action");
    }
    eq(stub_calls(), []);
});

t('workloads start and stop: owner builds only, the stop with the typed phrase, nothing but the verb reaches the controller', function () {
    stub_reset(['gate' => 'canary', 'mode' => 'owner']);
    [$code, $b] = post(['action' => 'workloads_stop']);
    eq([$code, $b['error']], [400, 'confirm']);
    [$code, $b] = post(['action' => 'workloads_stop', 'confirm' => 'STOP']);
    eq([$code, $b['error']], [400, 'confirm'], 'the generic phrase is not enough: the dev box goes down with it');
    eq($b['expected_hint'], 'STOP WORKLOADS');
    [$code] = post(['action' => 'workloads_stop', 'confirm' => 'STOP WORKLOADS']); eq($code, 200);
    [$code] = post(['action' => 'workloads_start']); eq($code, 200);
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), [['workloads-stop'], ['workloads-start']]);
    stub_reset(['gate' => 'canary', 'mode' => 'signed']);
    [$code, $b] = post(['action' => 'workloads_start']); eq([$code, $b['error']], [423, 'locked']);
    [$code] = post(['action' => 'workloads_stop', 'confirm' => 'STOP WORKLOADS']); eq($code, 423);
    stub_reset(['gate' => 'closed', 'mode' => 'owner']);
    [$code] = post(['action' => 'workloads_start']); eq($code, 423);
    stub_reset(['gate' => 'canary']);
    [$code] = post(['action' => 'workloads_start']); eq($code, 423, 'a controller without a mode is not an owner build');
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), []);
    stub_reset(['gate' => 'canary', 'mode' => 'owner']);
    [$code] = post(['action' => 'workloads_start', 'boot' => '1', 'wait' => '600', 'approve' => '1', 'args' => ['--approve']]); eq($code, 200);
    eq(array_values(array_filter(stub_calls(), fn($c) => $c[0] !== 'status')), [['workloads-start']]);
    eq(psu_confirm_phrase('workloads_stop'), 'STOP WORKLOADS');
});

t('the settings page has a Workloads tab', function () {
    $schema = psu_schema();
    $entries = [];
    foreach ($schema as $key => $rule) {
        $entries[] = ['key' => $key, 'type' => $rule['type'], 'tab' => $rule['tab'], 'title' => $rule['title'] ?? $key, 'label' => $rule['label'],
                      'values' => $rule['values'] ?? null, 'min' => $rule['min'] ?? null, 'max' => $rule['max'] ?? null,
                      'default' => $rule['default'], 'value' => $rule['default'], 'locked_while_running' => !empty($rule['locked_while_running'])];
    }
    $html = psu_render_settings_tabs($entries);
    ok(strpos($html, 'data-tab="workloads"') !== false && strpos($html, '>Workloads</button>') !== false);
    foreach (['WORKLOADS_ENABLED', 'WORKLOADS_INSTANCES', 'WORKLOADS_MEMORY_MB', 'WORKLOADS_CPUS'] as $key) {
        ok(strpos($html, 'name="' . $key . '"') !== false, $key);
    }
    ok(strpos($html, 'placeholder="e.g. ubuntudev"') !== false);
});

exit($failures === 0 ? 0 : 1);
