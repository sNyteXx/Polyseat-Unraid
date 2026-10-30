<?php
/* Dev harness only: render a .page body with a faked Unraid environment. Usage: php render.php PAGE THEME */
[$script, $page, $theme] = $argv;
$display = ['theme' => $theme];
$var = ['csrf_token' => 'TOKEN1234567890'];
$mytiles = [];
define('PSU_CTL', '/h/ctl.sh');
define('PSU_ASSET_DIR', '/usr/local/emhttp/plugins/polyseat-unraid');
$source = file_get_contents('/usr/local/emhttp/plugins/polyseat-unraid/' . basename($page));
$body = explode("\n---\n", $source, 2)[1];
file_put_contents('/tmp/page.php', $body);
ob_start();
include '/tmp/page.php';
$out = ob_get_clean();
echo isset($mytiles['polyseat-unraid']['column2']) ? $mytiles['polyseat-unraid']['column2'] : $out;
