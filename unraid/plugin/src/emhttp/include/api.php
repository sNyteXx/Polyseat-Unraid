<?php
declare(strict_types=1);
/* Thin endpoint; all logic lives in lib.php so it can be tested without a web server. */
require_once __DIR__ . '/lib.php';

try {
    [$status, $headers, $body] = psu_handle($_SERVER, $_GET, $_POST);
} catch (Throwable $e) {
    error_log('polyseat-unraid api: ' . $e->getMessage());
    [$status, $headers, $body] = [500, ['Content-Type' => 'application/json; charset=utf-8', 'Cache-Control' => 'no-store'],
                                  '{"ok":false,"error":"internal"}'];
}
http_response_code($status);
foreach ($headers as $name => $value) {
    header($name . ': ' . $value);
}
echo $body;
