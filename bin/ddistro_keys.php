<?php

// Fixed local CLI adapter to ChimServer's shared-key and settings managers; no duplicate encryption or SQL.

declare(strict_types=1);

use ChimServer\Config\Autoloader;
use ChimServer\Config\DBManager\DBManager;
use ChimServer\Core\ApiKeyManager\ApiKeyManager;
use ChimServer\Core\ApiKeyManager\ApiKeysService;
use ChimServer\Core\SettingsManager\SettingsManager;

if (PHP_SAPI !== 'cli' || count($argv) !== 3) {
    exit(1);
}

try {
    require $argv[1] . '/Config/Autoloader.php';
    Autoloader::register();
    $request = json_decode(stream_get_contents(STDIN, 16385), true, 16, JSON_THROW_ON_ERROR);
    if (!is_array($request) || (array_is_list($request) && $request !== [])) {
        throw new InvalidArgumentException();
    }
    $keys = ApiKeyManager::fromEnvironment();
    $settings = new SettingsManager(DBManager::fromEnvironment(), $keys);
    $service = new ApiKeysService($keys, $settings);
    $operation = $argv[2];
    $fields = match ($operation) {
        'status' => [],
        'create' => ['provider', 'label', 'origin', 'secret'],
        'edit' => ['key_id', 'revision', 'label', 'secret'],
        'test' => ['key_id'],
        'delete' => ['key_id', 'revision'],
        'bind' => ['connection_id', 'revision', 'key_id'],
        'import-environment' => ['connection_id', 'provider'],
        default => throw new InvalidArgumentException(),
    };
    if (array_diff(array_keys($request), $fields) !== []) {
        throw new InvalidArgumentException();
    }
    if (isset($request['key_id']) && !is_string($request['key_id'])) {
        throw new InvalidArgumentException();
    }
    foreach (['revision', 'connection_id'] as $integer) {
        if (isset($request[$integer]) && (!is_int($request[$integer]) || $request[$integer] < 1)) {
            throw new InvalidArgumentException();
        }
    }
    if ($operation === 'status') {
        $result = $service->listKeys();
        if ($result['outcome'] === 'listed') {
            $result['data']['connections'] = array_map(static fn (array $row): array => array_intersect_key($row,
                array_flip(['connection_id', 'label', 'kind', 'status', 'revision', 'api_key_id', 'credential_source'])), $settings->listConnections());
            $result['data']['warnings'] = ['Legacy HerikaServer, STOBE and Dialectic keys and assignments are preserved. They are not silently imported, merged or synchronized.'];
        }
    } else {
        $result = match ($operation) {
            'create' => $service->createKey($request),
            'edit' => $service->updateKey($request['key_id'], $request['revision'], array_intersect_key($request, array_flip(['label', 'secret']))),
            'test' => $service->testKey($request['key_id']),
            // Only CHIM 4 is connected today. Other products must join its usage check before binding.
            'delete' => $service->deleteKey($request['key_id'], $request['revision']),
            'bind' => $settings->updateConnection($request['connection_id'], $request['revision'], ['api_key_id' => $request['key_id'] ?? null]),
            'import-environment' => $settings->importConnectionCredential($request['connection_id'], $request['provider']),
        };
    }
    $success = in_array($result['outcome'], ['listed', 'created', 'updated', 'unchanged', 'deleted', 'tested', 'imported', 'already_imported'], true);
    // Public manager views only. An interrupted import may leave an unused key; retain its recovery ID.
    echo json_encode(['ok' => $success] + $result, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
} catch (Throwable) {
    echo json_encode(['ok' => false, 'outcome' => 'unavailable', 'error' => 'Shared key service is unavailable. Reload before retrying; an interrupted write may have completed.']) . "\n";
    exit(1);
}
