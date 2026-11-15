<?php

// Optional legacy-server bridge to CHIM 4 encrypted keys. Only provider dispatch may resolve a saved credential.

declare(strict_types=1);

use ChimServer\Config\Autoloader;
use ChimServer\Connectors\ProviderEndpoint;
use ChimServer\Core\ApiKeyManager\ApiKeyManager;
use ChimServer\Core\SettingsManager\SettingsRegistry;

final class DistroSharedKeys {
    public const REGISTRY = '/etc/dwemer/shared-key-consumers.json';
    /** Callers check this before relying on STT resolution or guarded saved-connector binding. */
    public const CAPABILITIES = ['stt', 'saved_connector_binding'];
    /**
     * Providers whose key may also serve speech-to-text, at the key's own exact origin. The manager keeps one immutable
     * kind per provider; STT reuses that stored kind only for these providers, never by retrying llm/tts or by label.
     */
    public const STT_PROVIDERS = ['openai', 'deepgram', 'inworld', 'custom_openai'];
    private const LEGACY = ['herika' => 'CHIM legacy', 'stobe' => 'STOBE', 'dialectic' => 'Dialectic'];
    private const SAVED_CONNECTORS = 'ex_saved_connectors';

    /** Preparation checks actual metadata-role access before adding a database to the deletion registry. */
    public static function checkSavedConnectorDatabase(string $database): void {
        self::loadConfiguration();
        if (preg_match('/^custom_[a-z0-9_]{1,55}$/D', $database) !== 1) {
            throw new RuntimeException('Invalid saved connector database');
        }
        $connection = self::connect($database);
        try {
            self::boundedRows($connection, 'SELECT id, kind, driver, enabled, shared_key_id FROM public.'
                . self::SAVED_CONNECTORS . ' WHERE shared_key_id IS NOT NULL LIMIT 129', null);
        } finally {
            pg_close($connection);
        }
    }

    /** Read legacy references using metadata-only grants; callers hold the shared key lock when authorizing deletion. */
    public static function consumerUsage(?string $keyId, ?array $storeIdentity): array {
        if ($keyId !== null && preg_match('/^[0-9a-f-]{36}$/D', $keyId) !== 1) {
            throw new RuntimeException('Shared key usage is unavailable');
        }
        $usage = [];
        foreach (self::registeredProducts($storeIdentity) as $product) {
            $connection = self::connect($product['database']);
            try {
                if ($product['id'] === 'soulkiller') {
                    // Saved connectors use UUID ids; disabled records still count, so deletion never strands them.
                    $query = 'SELECT id::text AS id, kind, driver, enabled, shared_key_id::text FROM public.'
                        . self::SAVED_CONNECTORS . ' WHERE shared_key_id IS NOT NULL'
                        . ($keyId === null ? '' : ' AND shared_key_id = $1::uuid') . ' ORDER BY id LIMIT 129';
                    foreach (self::boundedRows($connection, $query, $keyId) as $row) {
                        $enabled = $row['enabled'] === 't';
                        $usage[] = ['key_id' => $row['shared_key_id'], 'connection_id' => $row['id'],
                            'label' => 'SOULKILLER: ' . $row['driver'] . ' (' . $row['kind'] . ($enabled ? '' : ', disabled') . ')',
                            'status' => $enabled ? 'active' : 'disabled', 'product' => 'soulkiller', 'table' => self::SAVED_CONNECTORS];
                    }
                    continue;
                }
                foreach ($product['tables'] as $table) {
                    $displayColumn = $product['id'] === 'stobe' && $table !== 'core_api_badge' ? 'name' : 'label';
                    $query = 'SELECT id, ' . $displayColumn . ' AS label, shared_key_id::text FROM public.' . $table
                        . ' WHERE shared_key_id IS NOT NULL';
                    $query .= $keyId === null ? '' : ' AND shared_key_id = $1::uuid';
                    $query .= ' ORDER BY id LIMIT 129';
                    foreach (self::boundedRows($connection, $query, $keyId) as $row) {
                        $kind = ['core_api_badge' => 'key', 'core_llm_connector' => 'model', 'core_tts_connector' => 'voice'][$table];
                        $usage[] = ['key_id' => $row['shared_key_id'], 'connection_id' => (int)$row['id'],
                            'label' => self::LEGACY[$product['id']] . ': ' . $row['label'] . ' (' . $kind . ')', 'status' => 'active',
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
            [$keys, $origin] = self::manager($kind, $endpoint);
            $storedKind = self::storedKind($keys, $keyId, $kind);
            $result = $storedKind === null ? null : $keys->resolveSecret($keyId, $storedKind, $origin);
            if ($result === null || $result['outcome'] !== 'resolved') {
                throw new RuntimeException();
            }
            return $result['secret'];
        } catch (Throwable) {
            // Do not expose paths, environment, database diagnostics, previous plaintext or decrypted values.
            throw new RuntimeException('Shared API key unavailable for this provider endpoint');
        }
    }

    /**
     * SOULKILLER saved connectors: run $write() while the shared key row is share-locked, so a concurrent delete either
     * waits and then sees the committed reference or wins and nothing is written. $write must run one autocommitted
     * statement on $db that stores $keyId in the record, and return that record's UUID (null when nothing was written).
     * $db must be idle: a caller transaction would commit after this lock is released, invisible to the delete scan.
     * Returns ['outcome' => 'bound', 'id' => UUID] or a refusal outcome with 'id' => null: not_prepared, transaction_open,
     * missing, revoked, kind_mismatch, origin_mismatch, unsupported, conflict. Guard readability is checked before
     * writing, so a failed post-commit read never misreports a completed write as a retryable failure. Never performs HTTP.
     */
    public static function bindSavedConnector(\PgSql\Connection $db, string $keyId, string $kind, string $endpoint, callable $write): array {
        try {
            [$keys, $origin] = self::manager($kind, $endpoint);
            if (pg_transaction_status($db) !== PGSQL_TRANSACTION_IDLE) {
                return ['outcome' => 'transaction_open', 'id' => null];
            }
            $identity = self::storeIdentity();
            $product = null;
            foreach (self::registeredProducts($identity) as $candidate) {
                $product = $candidate['id'] === 'soulkiller' ? $candidate : $product;
            }
            // The caller's connection must be the exact registered database the deletion guard scans.
            if ($product === null || self::databaseIdentity($db) !== self::guardIdentity($product['database'])) {
                return ['outcome' => 'not_prepared', 'id' => null];
            }
            $storedKind = self::storedKind($keys, $keyId, $kind);
            if ($storedKind === null) {
                $found = $keys->getKey($keyId)['outcome'];
                return ['outcome' => $found === 'found' ? 'unsupported' : $found, 'id' => null];
            }
            $locked = $keys->withBindableKey($keyId, $storedKind, $origin, static function () use ($db, $keyId, $identity, $write): array {
                // The guard must be able to read every registered consumer before anything is written.
                self::consumerUsage($keyId, $identity);
                $id = $write();
                if (pg_transaction_status($db) !== PGSQL_TRANSACTION_IDLE) {
                    // A contract-violating callback must not commit later, after the key lock is gone.
                    @pg_query($db, 'ROLLBACK');
                    throw new RuntimeException();
                }
                if ($id === null) {
                    return ['outcome' => 'conflict', 'id' => null];
                }
                if (!is_string($id)) {
                    throw new RuntimeException();
                }
                return ['outcome' => 'bound', 'id' => $id];
            });
            return $locked['outcome'] === 'locked' ? $locked['result'] : ['outcome' => $locked['outcome'], 'id' => null];
        } catch (Throwable) {
            throw new RuntimeException('Shared API key binding unavailable');
        }
    }

    /** The stored manager kind that may serve $kind for this key, or null. llm/tts are exact; STT uses the allowlist. */
    private static function storedKind(ApiKeyManager $keys, string $keyId, string $kind): ?string {
        if ($kind !== 'stt') {
            return $kind;
        }
        // Provider and origin never change after creation, so this metadata read cannot race to another provider;
        // resolveSecret()/withBindableKey() re-check the stored kind, exact origin and active status themselves.
        $found = $keys->getKey($keyId);
        $provider = $found['outcome'] === 'found' ? $found['data']['provider'] : null;
        return in_array($provider, self::STT_PROVIDERS, true) ? (ApiKeyManager::PROVIDERS[$provider]['kind'] ?? null) : null;
    }

    /** The pinned manager and the canonical origin of $endpoint, for a supported kind only. */
    private static function manager(string $kind, string $endpoint): array {
        self::loadConfiguration();
        $runtime = is_file('/opt/dwemerdistro-keys/Config/Autoloader.php') ? '/opt/dwemerdistro-keys' : '/opt/ChimServer';
        require_once $runtime . '/Config/Autoloader.php';
        Autoloader::register();
        if (!in_array($kind, ['llm', 'tts', 'stt'], true) || !ProviderEndpoint::isValid($endpoint)) {
            throw new RuntimeException();
        }
        $origin = SettingsRegistry::endpointOrigin($endpoint);
        if ($origin === null) {
            throw new RuntimeException();
        }
        return [ApiKeyManager::fromEnvironment(), $origin];
    }

    /** The validated root-owned registry entries for the configured shared store; any doubt refuses. */
    private static function registeredProducts(?array $storeIdentity): array {
        $info = @lstat(self::REGISTRY);
        if ($info === false || ($info['mode'] & 0170000) !== 0100000 || $info['uid'] !== 0
            || ($info['mode'] & 0022) !== 0 || $info['size'] > 16384) {
            throw new RuntimeException('Shared key usage is unavailable');
        }
        $registry = json_decode((string)file_get_contents(self::REGISTRY), true, 8, JSON_THROW_ON_ERROR);
        if (!is_array($registry) || ($registry['version'] ?? null) !== 1 || !is_array($registry['shared'] ?? null)
            || !is_array($registry['products'] ?? null) || count($registry['products']) > 8
            || $storeIdentity === null || $registry['shared'] !== $storeIdentity || self::storeIdentity() !== $storeIdentity) {
            throw new RuntimeException('Shared key usage is unavailable');
        }
        $products = [];
        foreach ($registry['products'] as $product) {
            if (!is_array($product) || !in_array($product['id'] ?? null, [...array_keys(self::LEGACY), 'soulkiller'], true)
                || !is_string($product['database'] ?? null)
                || preg_match('/^[a-zA-Z_][a-zA-Z0-9_]{0,62}$/D', $product['database']) !== 1
                || isset($products[$product['id']])) {
                throw new RuntimeException('Shared key usage is unavailable');
            }
            $tables = $product['tables'] ?? ['core_api_badge'];
            $allowed = $product['id'] === 'soulkiller' ? [self::SAVED_CONNECTORS] : ['core_api_badge', 'core_llm_connector', 'core_tts_connector'];
            if (!is_array($tables) || count($tables) > 3 || count(array_unique($tables)) !== count($tables)
                || array_diff($tables, $allowed) !== [] || ($product['id'] === 'soulkiller' && $tables !== $allowed)) {
                throw new RuntimeException('Shared key usage is unavailable');
            }
            $products[$product['id']] = ['id' => $product['id'], 'database' => $product['database'], 'tables' => $tables];
        }
        return array_values($products);
    }

    /** The configured shared store identity; loads the fixed configuration first. */
    private static function storeIdentity(): array {
        self::loadConfiguration();
        return ['host' => (string)getenv('CHIM_SHARED_DB_HOST'), 'port' => (string)getenv('CHIM_SHARED_DB_PORT'),
            'database' => (string)getenv('CHIM_SHARED_DB_NAME')];
    }

    /** A fresh metadata-role connection to a registered product database on the shared store's server. */
    private static function connect(string $database) {
        $parts = ['host' => (string)getenv('CHIM_SHARED_DB_HOST'), 'port' => (string)getenv('CHIM_SHARED_DB_PORT'), 'dbname' => $database,
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
        return $connection;
    }

    /** At most 128 rows of a reference query; more, or any error, refuses. */
    private static function boundedRows($connection, string $query, ?string $keyId): array {
        $result = @pg_query_params($connection, $query, $keyId === null ? [] : [$keyId]);
        if ($result === false || pg_num_rows($result) > 128) {
            throw new RuntimeException('Shared key usage is unavailable');
        }
        return pg_fetch_all($result);
    }

    /** Server start time, port, database OID/name and saved-connector table OID as seen by one connection. */
    private static function databaseIdentity($connection): array {
        $result = @pg_query($connection, "SELECT pg_postmaster_start_time()::text AS started, current_setting('port') AS port,"
            . " current_database() AS database, (SELECT oid::text FROM pg_database WHERE datname = current_database()) AS oid,"
            . " to_regclass('public." . self::SAVED_CONNECTORS . "')::oid::text AS saved_connectors,"
            . " to_regclass('" . self::SAVED_CONNECTORS . "')::oid::text AS resolved_connectors");
        $row = $result === false ? false : pg_fetch_assoc($result);
        if ($row === false || $row['saved_connectors'] === null || $row['resolved_connectors'] !== $row['saved_connectors']) {
            throw new RuntimeException();
        }
        return $row;
    }

    private static function guardIdentity(string $database): array {
        $connection = self::connect($database);
        try {
            return self::databaseIdentity($connection);
        } finally {
            pg_close($connection);
        }
    }

    /** Read the server's fixed literal shared-store settings, never evaluate shell syntax or accept caller overrides. */
    private static function loadConfiguration(): void {
        $values = [];
        foreach (['/etc/chim4-s5.env', '/etc/chim4-dialogue.env', '/etc/chim4-tts.env', '/etc/dwemer/shared-keys.env'] as $file) {
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
