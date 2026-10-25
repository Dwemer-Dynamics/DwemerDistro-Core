<?php

// Optional legacy-server bridge to CHIM 4 encrypted keys. Only provider dispatch may resolve a saved credential.

declare(strict_types=1);

use ChimServer\Config\Autoloader;
use ChimServer\Connectors\ProviderEndpoint;
use ChimServer\Core\ApiKeyManager\ApiKeyManager;
use ChimServer\Core\SettingsManager\SettingsRegistry;

final class DistroSharedKeys {
    public const REGISTRY = '/etc/dwemer/shared-key-consumers.json';

    /** Read legacy references using metadata-only grants; callers hold the shared key lock when authorizing deletion. */
    public static function consumerUsage(?string $keyId, ?array $storeIdentity): array {
        $info = @lstat(self::REGISTRY);
        if ($info === false || ($info['mode'] & 0170000) !== 0100000 || $info['uid'] !== 0
            || ($info['mode'] & 0022) !== 0 || $info['size'] > 16384) {
            throw new RuntimeException('Shared key usage is unavailable');
        }
        $registry = json_decode((string)file_get_contents(self::REGISTRY), true, 8, JSON_THROW_ON_ERROR);
        if (!is_array($registry) || ($registry['version'] ?? null) !== 1 || !is_array($registry['shared'] ?? null)
            || !is_array($registry['products'] ?? null) || count($registry['products']) > 8
            || $storeIdentity === null || $registry['shared'] !== $storeIdentity) {
            throw new RuntimeException('Shared key usage is unavailable');
        }
        self::loadConfiguration();
        $configured = ['host' => (string)getenv('CHIM_SHARED_DB_HOST'), 'port' => (string)getenv('CHIM_SHARED_DB_PORT'),
            'database' => (string)getenv('CHIM_SHARED_DB_NAME')];
        if ($configured !== $storeIdentity || ($keyId !== null && preg_match('/^[0-9a-f-]{36}$/D', $keyId) !== 1)) {
            throw new RuntimeException('Shared key usage is unavailable');
        }
        $usage = [];
        $seen = [];
        foreach ($registry['products'] as $product) {
            if (!is_array($product) || !in_array($product['id'] ?? null, ['herika', 'stobe', 'dialectic'], true)
                || !is_string($product['database'] ?? null)
                || preg_match('/^[a-zA-Z_][a-zA-Z0-9_]{0,62}$/D', $product['database']) !== 1
                || isset($seen[$product['id']])) {
                throw new RuntimeException('Shared key usage is unavailable');
            }
            $seen[$product['id']] = true;
            $parts = ['host' => $configured['host'], 'port' => $configured['port'], 'dbname' => $product['database'],
                'user' => (string)getenv('CHIM_SHARED_DB_USER'), 'password' => (string)getenv('CHIM_SHARED_DB_PASSWORD'),
                'connect_timeout' => '3', 'options' => '-c statement_timeout=3000 -c lock_timeout=1000'];
            $conninfo = [];
            foreach ($parts as $name => $value) {
                $conninfo[] = $name . "='" . str_replace(['\\', "'"], ['\\\\', "\\'"], $value) . "'";
            }
            $connection = @pg_connect(implode(' ', $conninfo), PGSQL_CONNECT_FORCE_NEW);
            if ($connection === false) {
                throw new RuntimeException('Shared key usage is unavailable');
            }
            try {
                $tables = $product['tables'] ?? ['core_api_badge'];
                if (!is_array($tables) || count($tables) > 3 || count(array_unique($tables)) !== count($tables)) {
                    throw new RuntimeException('Shared key usage is unavailable');
                }
                foreach ($tables as $table) {
                    if (!in_array($table, ['core_api_badge', 'core_llm_connector', 'core_tts_connector'], true)) {
                        throw new RuntimeException('Shared key usage is unavailable');
                    }
                    $displayColumn = $product['id'] === 'stobe' && $table !== 'core_api_badge' ? 'name' : 'label';
                    $query = 'SELECT id, ' . $displayColumn . ' AS label, shared_key_id::text FROM public.' . $table
                        . ' WHERE shared_key_id IS NOT NULL';
                    $query .= $keyId === null ? '' : ' AND shared_key_id = $1::uuid';
                    $query .= ' ORDER BY id LIMIT 129';
                    $result = @pg_query_params($connection, $query, $keyId === null ? [] : [$keyId]);
                    if ($result === false || pg_num_rows($result) > 128) {
                        throw new RuntimeException('Shared key usage is unavailable');
                    }
                    foreach (pg_fetch_all($result) as $row) {
                        $label = ['herika' => 'CHIM legacy', 'stobe' => 'STOBE', 'dialectic' => 'Dialectic'][$product['id']];
                        $kind = ['core_api_badge' => 'key', 'core_llm_connector' => 'model', 'core_tts_connector' => 'voice'][$table];
                        $usage[] = ['key_id' => $row['shared_key_id'], 'connection_id' => (int)$row['id'],
                            'label' => $label . ': ' . $row['label'] . ' (' . $kind . ')', 'status' => 'active',
                            'product' => $product['id'], 'table' => $table];
                    }
                }
            } finally {
                pg_close($connection);
            }
        }
        return $usage;
    }

    /** Resolve for the actual outbound endpoint, refusing every failure without reading a legacy copied value. */
    public static function resolve(string $keyId, string $kind, string $endpoint): string {
        try {
            self::loadConfiguration();
            require_once '/opt/ChimServer/Config/Autoloader.php';
            Autoloader::register();
            if (!in_array($kind, ['llm', 'tts'], true) || !ProviderEndpoint::isValid($endpoint)) {
                throw new RuntimeException();
            }
            $origin = SettingsRegistry::endpointOrigin($endpoint);
            if ($origin === null) {
                throw new RuntimeException();
            }
            $result = ApiKeyManager::fromEnvironment()->resolveSecret($keyId, $kind, $origin);
            if ($result['outcome'] !== 'resolved') {
                throw new RuntimeException();
            }
            return $result['secret'];
        } catch (Throwable) {
            // Do not expose paths, environment, database diagnostics, previous plaintext or decrypted values.
            throw new RuntimeException('Shared API key unavailable for this provider endpoint');
        }
    }

    /** Read the server's fixed literal shared-store settings, never evaluate shell syntax or accept caller overrides. */
    private static function loadConfiguration(): void {
        $values = [];
        foreach (['/etc/chim4-s5.env', '/etc/chim4-dialogue.env', '/etc/chim4-tts.env'] as $file) {
            if (!file_exists($file)) {
                continue;
            }
            $info = lstat($file);
            if ($info === false || ($info['mode'] & 0170000) !== 0100000 || $info['uid'] !== 0
                || ($info['mode'] & 0022) !== 0 || $info['size'] > 1048576) {
                throw new RuntimeException();
            }
            $lines = file($file, FILE_IGNORE_NEW_LINES);
            if ($lines === false) {
                throw new RuntimeException();
            }
            foreach ($lines as $line) {
                if (trim($line) === '' || str_starts_with(ltrim($line), '#')) {
                    continue;
                }
                if (preg_match('/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/D', rtrim($line, "\r"), $match) !== 1) {
                    throw new RuntimeException();
                }
                if (!str_starts_with($match[1], 'CHIM_SHARED_DB_') && $match[1] !== 'CHIM_API_KEY_FILE') {
                    continue;
                }
                $value = $match[2];
                $trimmed = rtrim($value);
                if (strlen($trimmed) >= 2 && in_array($trimmed[0], ['"', "'"], true)
                    && str_ends_with($trimmed, $trimmed[0])) {
                    $value = substr($trimmed, 1, -1);
                }
                $values[$match[1]] = $value;
            }
        }
        foreach (['CHIM_SHARED_DB_HOST', 'CHIM_SHARED_DB_PORT', 'CHIM_SHARED_DB_NAME', 'CHIM_SHARED_DB_USER',
            'CHIM_SHARED_DB_PASSWORD', 'CHIM_API_KEY_FILE'] as $name) {
            // A missing configured value must not revive an inherited credential or a different database.
            putenv($name . '=' . ($values[$name] ?? ''));
        }
    }
}
