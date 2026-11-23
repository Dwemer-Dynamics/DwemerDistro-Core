<?php

// Optional legacy-server bridge to CHIM 4 encrypted keys. Only provider dispatch may resolve a saved credential.

declare(strict_types=1);

use ChimServer\Config\Autoloader;
use ChimServer\Connectors\ProviderEndpoint;
use ChimServer\Core\ApiKeyManager\ApiKeyManager;
use ChimServer\Core\SettingsManager\SettingsRegistry;

final class DistroSharedKeys {
    public const REGISTRY = '/etc/dwemer/shared-key-consumers.json';
    /**
     * Callers check this before relying on STT resolution or guarded saved-connector binding. chevo_badge_binding is
     * CHEVO's whole contract: bindChevoBadge, resolveChevo (including itt) and chevoKeyCatalog.
     */
    public const CAPABILITIES = ['stt', 'saved_connector_binding', 'chevo_badge_binding'];
    /**
     * Providers whose key may also serve speech-to-text, at the key's own exact origin. The manager keeps one immutable
     * kind per provider; STT reuses that stored kind only for these providers, never by retrying llm/tts or by label.
     */
    public const STT_PROVIDERS = ['openai', 'deepgram', 'inworld', 'custom_openai'];
    /**
     * CHEVO only: providers whose key may serve image-to-text, for CHEVO's chat-completions vision drivers openrouter,
     * openai and local (an OpenAI-compatible service registered as custom_openai). Same stored-kind and exact-origin
     * rules as STT; resolve() and bindSavedConnector() never accept itt.
     */
    public const ITT_PROVIDERS = ['openrouter', 'openai', 'custom_openai'];
    private const LEGACY = ['herika' => 'CHIM legacy', 'stobe' => 'STOBE', 'dialectic' => 'Dialectic'];
    private const SAVED_CONNECTORS = 'ex_saved_connectors';
    /** SYNTH publishes the shared keys its connectors use through this view of its own database (migration Version20261116100000). */
    private const SYNTH_REFERENCES = 'synth_key_references';
    private const SYNTH_DATABASE = 'synth';
    /** CHEVO's own migration publishes its badge references here (id, label, shared_key_id of core_api_badge only). */
    private const CHEVO_REFERENCES = 'chevo_key_references';
    private const CHEVO_BADGES = 'core_api_badge';

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

    /** SYNTH readiness: the metadata role must read its reference view in the one fixed database; never touches other tables. */
    public static function checkSynthDatabase(): void {
        self::loadConfiguration();
        $connection = self::connect(self::SYNTH_DATABASE);
        try {
            self::boundedRows($connection, 'SELECT id, kind, label, shared_key_id FROM public.' . self::SYNTH_REFERENCES
                . ' WHERE shared_key_id IS NOT NULL LIMIT 129', null);
        } finally {
            pg_close($connection);
        }
    }

    /** CHEVO readiness: the metadata role must read the badge reference view of the one database being enrolled. */
    public static function checkChevoDatabase(string $database): void {
        self::loadConfiguration();
        if (preg_match('/^[a-z_][a-z0-9_]{0,62}$/D', $database) !== 1) {
            throw new RuntimeException('Invalid CHEVO database');
        }
        $connection = self::connect($database);
        try {
            self::boundedRows($connection, 'SELECT id, label, shared_key_id FROM public.' . self::CHEVO_REFERENCES
                . ' WHERE shared_key_id IS NOT NULL LIMIT 129', null);
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
                if ($product['id'] === 'synth') {
                    // One row per connector that references a shared key: model (LLM), voice (TTS), stt and itt.
                    $query = 'SELECT id, kind, label, shared_key_id::text FROM public.' . self::SYNTH_REFERENCES
                        . ' WHERE shared_key_id IS NOT NULL'
                        . ($keyId === null ? '' : ' AND shared_key_id = $1::uuid') . ' ORDER BY kind, id LIMIT 129';
                    foreach (self::boundedRows($connection, $query, $keyId) as $row) {
                        $usage[] = ['key_id' => $row['shared_key_id'], 'connection_id' => (int)$row['id'],
                            'label' => 'SYNTH: ' . $row['label'] . ' (' . $row['kind'] . ')', 'status' => 'active',
                            'product' => 'synth', 'table' => self::SYNTH_REFERENCES];
                    }
                    continue;
                }
                if ($product['id'] === 'chevo') {
                    // Badges have no enabled state: every badge holding a reference counts, used by a connector or not.
                    $query = 'SELECT id, label, shared_key_id::text FROM public.' . self::CHEVO_REFERENCES
                        . ' WHERE shared_key_id IS NOT NULL'
                        . ($keyId === null ? '' : ' AND shared_key_id = $1::uuid') . ' ORDER BY id LIMIT 129';
                    foreach (self::boundedRows($connection, $query, $keyId) as $row) {
                        $usage[] = ['key_id' => $row['shared_key_id'], 'connection_id' => (int)$row['id'],
                            'label' => 'CHEVO: ' . $row['label'] . ' (key)', 'status' => 'active',
                            'product' => 'chevo', 'table' => self::CHEVO_REFERENCES];
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
            if (!in_array($kind, ['llm', 'tts', 'stt'], true)) {
                throw new RuntimeException();
            }
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
            if (!in_array($kind, ['llm', 'tts', 'stt'], true)) {
                throw new RuntimeException();
            }
            [$keys, $origin] = self::manager($kind, $endpoint);
            $authorize = static function () use ($keys, $keyId, $kind): array {
                $storedKind = self::storedKind($keys, $keyId, $kind);
                if ($storedKind !== null) {
                    return ['authorized', $storedKind];
                }
                $found = $keys->getKey($keyId)['outcome'];
                return [$found === 'found' ? 'unsupported' : $found, null];
            };
            return self::bindReference('soulkiller', [self::SAVED_CONNECTORS], $db, $keys, $keyId, $origin, null, $authorize, $write);
        } catch (Throwable) {
            throw new RuntimeException('Shared API key binding unavailable');
        }
    }

    /**
     * CHEVO badges: the bindSavedConnector contract for one core_api_badge row of the registered CHEVO database, on
     * CHEVO's own Doctrine pdo_pgsql connection ($entityManager->getConnection()->getNativeConnection()); no second
     * writer connection is opened. $provider is the provider CHEVO's driver policy requires for the connector the badge
     * is chosen for, and $kind (llm, tts, stt or itt) and $endpoint are that connector's kind and saved URL; the key's
     * stored provider must equal $provider exactly, may serve $kind and must be bound to $endpoint's exact origin.
     * $write must run one autocommitted statement on $db that sets that badge's shared_key_id to $keyId and return its
     * integer id (null when nothing was written). $db must be idle, on the registered database and server, and resolve
     * core_api_badge and the reference view to public. Work $write leaves open is rolled back. Returns 'bound' only once
     * the guard's own connection reads the committed reference while the key lock is still held; 'unknown' (or an
     * exception once $write may have run) means the write may have completed: reload, never retry. Other outcomes are
     * the bindSavedConnector refusals plus provider_mismatch, all with nothing written. Never performs HTTP.
     */
    public static function bindChevoBadge(\PDO $db, string $keyId, string $provider, string $kind, string $endpoint, callable $write): array {
        try {
            if ($db->getAttribute(\PDO::ATTR_DRIVER_NAME) !== 'pgsql') {
                throw new RuntimeException();
            }
            [$keys, $origin] = self::manager($kind, $endpoint);
            $authorize = static fn (): array => self::chevoAuthorize($keys, $keyId, $provider, $kind, $origin);
            return self::bindReference('chevo', [self::CHEVO_REFERENCES, self::CHEVO_BADGES], $db, $keys, $keyId, $origin,
                $provider, $authorize, $write);
        } catch (Throwable) {
            throw new RuntimeException('Shared API key binding unavailable');
        }
    }

    /**
     * CHEVO dispatch: the secret of $keyId for $kind at $endpoint, only when the key's stored provider is exactly the
     * $provider CHEVO's driver policy requires for that connector. Same refusals and redaction as resolve().
     */
    public static function resolveChevo(string $keyId, string $provider, string $kind, string $endpoint): string {
        try {
            [$keys, $origin] = self::manager($kind, $endpoint);
            [$outcome, $storedKind] = self::chevoAuthorize($keys, $keyId, $provider, $kind, $origin);
            $result = $outcome === 'authorized' ? $keys->resolveSecret($keyId, $storedKind, $origin) : null;
            if ($result === null || $result['outcome'] !== 'resolved') {
                throw new RuntimeException();
            }
            return $result['secret'];
        } catch (Throwable) {
            throw new RuntimeException('Shared API key unavailable for this provider endpoint');
        }
    }

    /**
     * CHEVO key picker: metadata of the active global keys, each key_id, provider, label and origin only. Never reads a
     * secret, a masked preview or any diagnostic; any failure throws one fixed message. Never performs HTTP.
     */
    public static function chevoKeyCatalog(): array {
        try {
            self::loadRuntime();
            $listed = ApiKeyManager::fromEnvironment()->listKeys();
            if ($listed['outcome'] !== 'listed') {
                throw new RuntimeException();
            }
            $keys = [];
            foreach ($listed['data']['keys'] as $key) {
                $keys[] = ['key_id' => (string)$key['key_id'], 'provider' => (string)$key['provider'],
                    'label' => (string)$key['label'], 'origin' => (string)$key['origin']];
            }
            return $keys;
        } catch (Throwable) {
            throw new RuntimeException('Shared API keys unavailable');
        }
    }

    /**
     * Shared guarded write for one registered product. $relations must resolve identically for the caller and the guard.
     * $authorize returns ['authorized', stored kind] or [refusal outcome, null] from immutable key metadata, before the
     * pinned manager locks the key; a non-null $provider is checked again against the locked row.
     */
    private static function bindReference(string $productId, array $relations, \PgSql\Connection|\PDO $db, ApiKeyManager $keys,
        string $keyId, string $origin, ?string $provider, callable $authorize, callable $write): array {
        try {
            if (!self::idle($db)) {
                return ['outcome' => 'transaction_open', 'id' => null];
            }
            $identity = self::storeIdentity();
            $product = null;
            foreach (self::registeredProducts($identity) as $candidate) {
                $product = $candidate['id'] === $productId ? $candidate : $product;
            }
            // The caller's connection must be the exact registered database the deletion guard scans.
            if ($product === null || self::databaseIdentity($db, $relations) !== self::guardIdentity($product['database'], $relations)) {
                return ['outcome' => 'not_prepared', 'id' => null];
            }
            [$outcome, $storedKind] = $authorize();
            if ($outcome !== 'authorized') {
                return ['outcome' => $outcome, 'id' => null];
            }
            $database = $product['database'];
            $locked = $keys->withBindableKey($keyId, $storedKind, $origin, static function (array $row) use ($db, $keyId, $identity, $write, $productId, $database, $provider): array {
                if ($provider !== null && ($row['provider'] ?? null) !== $provider) {
                    return ['outcome' => 'provider_mismatch', 'id' => null];
                }
                // The guard must be able to read every registered consumer before anything is written.
                self::consumerUsage($keyId, $identity);
                try {
                    $id = $write();
                } finally {
                    // A contract-violating callback must not commit later, after the key lock is gone.
                    $open = !self::idle($db);
                    if ($open) {
                        self::rollback($db);
                    }
                }
                if ($open) {
                    throw new RuntimeException();
                }
                if ($id === null) {
                    return ['outcome' => 'conflict', 'id' => null];
                }
                if ($productId !== 'chevo') {
                    if (!is_string($id)) {
                        throw new RuntimeException();
                    }
                    return ['outcome' => 'bound', 'id' => $id];
                }
                if (!is_int($id) || $id < 1) {
                    throw new RuntimeException();
                }
                // A concurrent delete scans with its own connection after this lock is released: prove it will see this row.
                return self::chevoReferenceVisible($database, $id, $keyId)
                    ? ['outcome' => 'bound', 'id' => $id] : ['outcome' => 'unknown', 'id' => null];
            });
            return $locked['outcome'] === 'locked' ? $locked['result'] : ['outcome' => $locked['outcome'], 'id' => null];
        } catch (Throwable) {
            throw new RuntimeException('Shared API key binding unavailable');
        }
    }

    /** Whether the guard's metadata connection reads badge $id referencing $keyId in the registered CHEVO database. */
    private static function chevoReferenceVisible(string $database, int $id, string $keyId): bool {
        try {
            $connection = self::connect($database);
        } catch (Throwable) {
            return false;
        }
        try {
            $result = @pg_query_params($connection, 'SELECT 1 FROM public.' . self::CHEVO_REFERENCES
                . ' WHERE id = $1::integer AND shared_key_id = $2::uuid', [$id, $keyId]);
            return $result !== false && pg_num_rows($result) === 1;
        } finally {
            pg_close($connection);
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

    /**
     * CHEVO: ['authorized', stored kind] when $provider may serve $kind and the key's immutable metadata names exactly
     * $provider at $origin; otherwise [unsupported|missing|revoked|provider_mismatch|origin_mismatch, null]. The pinned
     * manager still re-checks the stored kind, exact origin and active status when it resolves or locks the key.
     */
    private static function chevoAuthorize(ApiKeyManager $keys, string $keyId, string $provider, string $kind, string $origin): array {
        $capable = match ($kind) {
            'stt' => in_array($provider, self::STT_PROVIDERS, true),
            'itt' => in_array($provider, self::ITT_PROVIDERS, true),
            'llm', 'tts' => (ApiKeyManager::PROVIDERS[$provider]['kind'] ?? null) === $kind,
            default => false,
        };
        $storedKind = ApiKeyManager::PROVIDERS[$provider]['kind'] ?? null;
        if (!$capable || !is_string($storedKind)) {
            return ['unsupported', null];
        }
        $found = $keys->getKey($keyId);
        if ($found['outcome'] !== 'found') {
            return [$found['outcome'] === 'revoked' ? 'revoked' : 'missing', null];
        }
        if ($found['data']['provider'] !== $provider) {
            return ['provider_mismatch', null];
        }
        if ($found['data']['origin'] !== $origin) {
            return ['origin_mismatch', null];
        }
        return ['authorized', $storedKind];
    }

    /** Whether the caller's connection has no transaction open (pdo_pgsql reports the server's transaction status). */
    private static function idle(\PgSql\Connection|\PDO $db): bool {
        if ($db instanceof \PDO) {
            return !$db->inTransaction();
        }
        return pg_transaction_status($db) === PGSQL_TRANSACTION_IDLE;
    }

    /** Best-effort rollback of work a callback left open on the caller's connection. */
    private static function rollback(\PgSql\Connection|\PDO $db): void {
        if (!($db instanceof \PDO)) {
            @pg_query($db, 'ROLLBACK');
            return;
        }
        try {
            $db->rollBack();
        } catch (Throwable) {
        }
        try {
            if ($db->inTransaction()) {
                @$db->exec('ROLLBACK');
            }
        } catch (Throwable) {
        }
    }

    /** The pinned manager and the canonical origin of $endpoint, for a supported kind only. */
    private static function manager(string $kind, string $endpoint): array {
        self::loadRuntime();
        if (!in_array($kind, ['llm', 'tts', 'stt', 'itt'], true) || !ProviderEndpoint::isValid($endpoint)) {
            throw new RuntimeException();
        }
        $origin = SettingsRegistry::endpointOrigin($endpoint);
        if ($origin === null) {
            throw new RuntimeException();
        }
        return [ApiKeyManager::fromEnvironment(), $origin];
    }

    /** The fixed shared-store configuration and the pinned manager runtime. */
    private static function loadRuntime(): void {
        self::loadConfiguration();
        $runtime = is_file('/opt/dwemerdistro-keys/Config/Autoloader.php') ? '/opt/dwemerdistro-keys' : '/opt/ChimServer';
        require_once $runtime . '/Config/Autoloader.php';
        Autoloader::register();
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
            if (!is_array($product) || !in_array($product['id'] ?? null, [...array_keys(self::LEGACY), 'soulkiller', 'synth', 'chevo'], true)
                || !is_string($product['database'] ?? null)
                || preg_match('/^[a-zA-Z_][a-zA-Z0-9_]{0,62}$/D', $product['database']) !== 1
                || isset($products[$product['id']])) {
                throw new RuntimeException('Shared key usage is unavailable');
            }
            $tables = $product['tables'] ?? ['core_api_badge'];
            $allowed = match ($product['id']) {
                'soulkiller' => [self::SAVED_CONNECTORS],
                'synth' => [self::SYNTH_REFERENCES],
                'chevo' => [self::CHEVO_REFERENCES],
                default => ['core_api_badge', 'core_llm_connector', 'core_tts_connector'],
            };
            if (!is_array($tables) || count($tables) > 3 || count(array_unique($tables)) !== count($tables)
                || array_diff($tables, $allowed) !== [] || (in_array($product['id'], ['soulkiller', 'synth', 'chevo'], true) && $tables !== $allowed)) {
                throw new RuntimeException('Shared key usage is unavailable');
            }
            if ($product['id'] === 'synth' && $product['database'] !== self::SYNTH_DATABASE) {
                throw new RuntimeException('Shared key usage is unavailable');
            }
            // CHEVO's database is configurable: enrollment validates it, and it may never alias another consumer or the store.
            if ($product['id'] === 'chevo' && (preg_match('/^[a-z_][a-z0-9_]{0,62}$/D', $product['database']) !== 1
                || in_array($product['database'], ['postgres', 'template0', 'template1', self::SYNTH_DATABASE, $storeIdentity['database']], true)
                || str_starts_with($product['database'], 'custom_'))) {
                throw new RuntimeException('Shared key usage is unavailable');
            }
            $products[$product['id']] = ['id' => $product['id'], 'database' => $product['database'], 'tables' => $tables];
        }
        if (isset($products['chevo']) && count(array_keys(array_column($products, 'database'), $products['chevo']['database'], true)) !== 1) {
            throw new RuntimeException('Shared key usage is unavailable');
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

    /**
     * Server start time, port, database OID/name and the OIDs of fixed $relations as seen by one connection, a pgsql
     * connection or a pdo_pgsql PDO. Each relation must exist in public and resolve there unqualified too, so a
     * shadowing search path is refused. The start time is an epoch so session TimeZone/DateStyle cannot change it, and
     * columns are read by position so PDO name-case or null attributes cannot change the comparison.
     */
    private static function databaseIdentity(\PgSql\Connection|\PDO $connection, array $relations): array {
        $columns = '';
        foreach (array_values($relations) as $relation) {
            $columns .= ", to_regclass('public." . $relation . "')::oid::text, to_regclass('" . $relation . "')::oid::text";
        }
        $query = "SELECT extract(epoch FROM pg_postmaster_start_time())::text, current_setting('port'),"
            . " current_database()::text, (SELECT oid::text FROM pg_database WHERE datname = current_database())" . $columns;
        if ($connection instanceof \PDO) {
            $statement = @$connection->query($query);
            $row = $statement === false ? false : $statement->fetch(\PDO::FETCH_NUM);
            if ($statement !== false) {
                $statement->closeCursor();
            }
        } else {
            $result = @pg_query($connection, $query);
            $row = $result === false ? false : pg_fetch_row($result);
        }
        if (!is_array($row) || count($row) !== 4 + 2 * count($relations)) {
            throw new RuntimeException();
        }
        foreach ($row as $index => $value) {
            if (!is_string($value) || $value === '' || ($index >= 4 && $index % 2 === 1 && $value !== $row[$index - 1])) {
                throw new RuntimeException();
            }
        }
        return $row;
    }

    private static function guardIdentity(string $database, array $relations): array {
        $connection = self::connect($database);
        try {
            return self::databaseIdentity($connection, $relations);
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
