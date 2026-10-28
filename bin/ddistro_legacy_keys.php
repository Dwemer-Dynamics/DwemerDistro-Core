<?php

// Explicit legacy badge migration/binding. Core supplies a private snapshot; no credential or assignment is guessed.

declare(strict_types=1);

use ChimServer\Core\ApiKeyManager\ApiKeyManager;

final class DistroLegacyKeys {
    /** Keep the root-local snapshot private; its secret is used only for encryption and the database compare-and-swap. */
    public function __construct(private readonly ApiKeyManager $keys, #[\SensitiveParameter] private readonly array $source) {
    }

    /** Create one distinct encrypted key, retaining every legacy ID and assignment; report unused-key recovery on failure. */
    public function importKey(#[\SensitiveParameter] array $fields): array {
        if ($this->source['shared_key_id'] !== null) {
            $found = $this->keys->getKey($this->source['shared_key_id']);
            return ['outcome' => $found['outcome'] === 'found' ? 'already_imported' : 'credential_unavailable', 'data' => $found['data']];
        }
        $secret = $this->source['secret'];
        if ($secret === '') {
            return ['outcome' => 'invalid', 'data' => null, 'errors' => [['key' => 'secret', 'reason' => 'legacy_key_empty']]];
        }
        $provider = $fields['provider'] ?? null;
        $definition = is_string($provider) ? (ApiKeyManager::PROVIDERS[$provider] ?? null) : null;
        $origin = $definition['origin'] ?? ApiKeyManager::canonicalOrigin($fields['origin'] ?? null);
        if ($definition !== null && $origin !== null && !$this->compatible($definition['kind'], $origin)) {
            return ['outcome' => 'invalid', 'data' => null,
                'errors' => [['key' => 'provider', 'reason' => 'legacy_provider_or_origin_mismatch']]];
        }
        $created = $this->keys->createKey($fields['provider'] ?? null, $fields['label'] ?? null,
            $fields['origin'] ?? null, $secret);
        if ($created['outcome'] !== 'created') {
            return $created;
        }
        $keyId = $created['data']['key_id'];
        try {
            $bound = $this->bind($keyId);
        } catch (Throwable) {
            return ['outcome' => 'bind_unknown', 'data' => ['key_id' => $keyId, 'reload_required' => true]];
        }
        return ['outcome' => $bound['outcome'] === 'bound' ? 'imported' : 'bind_failed',
            'data' => ['key_id' => $keyId, 'reload_required' => $bound['outcome'] !== 'bound']];
    }

    /** Bind under the shared-row lock, then commit the legacy compare-and-swap before releasing that lock. */
    public function bind(?string $keyId): array {
        if ($keyId === null) {
            return ['outcome' => $this->writeReference(null) ? 'detached' : 'conflict', 'data' => null];
        }
        $found = $this->keys->getKey($keyId);
        if ($found['outcome'] !== 'found') {
            return ['outcome' => $found['outcome'], 'data' => null];
        }
        $key = $found['data'];
        if (!$this->compatible($key['kind'], $key['origin'])) {
            return ['outcome' => 'invalid', 'data' => null,
                'errors' => [['key' => 'key_id', 'reason' => 'legacy_provider_or_origin_mismatch']]];
        }
        $result = $this->keys->withBindableKey($keyId, $key['kind'], $key['origin'], fn (array $row): bool => $this->writeReference($row['key_id']));
        return ['outcome' => $result['outcome'] === 'locked' ? ($result['result'] ? 'bound' : 'conflict') : $result['outcome'], 'data' => null];
    }

    /** Every existing user of this badge must keep the same supported provider category and network origin. */
    private function compatible(string $kind, string $origin): bool {
        foreach ($this->source['endpoints'] as $endpoint) {
            if ($endpoint['kind'] !== $kind || $endpoint['origin'] !== $origin) {
                return false;
            }
        }
        return true;
    }

    /** Execute the owner-installed CAS with the metadata role; errors never include SQL parameters or database detail. */
    private function writeReference(?string $keyId): bool {
        $parts = ['host' => (string)getenv('CHIM_SHARED_DB_HOST'), 'port' => (string)getenv('CHIM_SHARED_DB_PORT'),
            'dbname' => $this->source['database'], 'user' => (string)getenv('CHIM_SHARED_DB_USER'),
            'password' => (string)getenv('CHIM_SHARED_DB_PASSWORD'), 'connect_timeout' => '3',
            'options' => '-c statement_timeout=3000 -c lock_timeout=1000'];
        $conninfo = [];
        foreach ($parts as $name => $value) {
            $conninfo[] = $name . "='" . str_replace(['\\', "'"], ['\\\\', "\\'"], $value) . "'";
        }
        $connection = @pg_connect(implode(' ', $conninfo), PGSQL_CONNECT_FORCE_NEW);
        if ($connection === false) {
            throw new RuntimeException('Legacy key binding unavailable');
        }
        try {
            $result = @pg_query_params($connection,
                'SELECT public.ddistro_bind_shared_key($1::text,$2::integer,$3::text,$4::uuid,$5::uuid,$6::oid) AS changed',
                [$this->source['table'], $this->source['id'], $this->source['etag'],
                    $this->source['shared_key_id'], $keyId, $this->source['generation']]);
            if ($result === false) {
                throw new RuntimeException('Legacy key binding unavailable');
            }
            return pg_fetch_result($result, 0, 'changed') === 't';
        } finally {
            pg_close($connection);
        }
    }
}
