<?php

// Fixed local CLI adapter to ChimServer's shared-key and settings managers; no duplicate encryption or SQL.

declare(strict_types=1);

use ChimServer\Config\Autoloader;
use ChimServer\Connectors\ApiKeyCheck;
use ChimServer\Config\DBManager\DBManager;
use ChimServer\Core\ApiKeyManager\ApiKeyManager;
use ChimServer\Core\ApiKeyManager\ApiKeysService;
use ChimServer\Core\SettingsManager\SettingsManager;
use ChimServer\Core\SettingsManager\SettingsRegistry;

/** Save one connection without changing another connection that happens to share its old encrypted key. */
function saveConnectionKey(ApiKeyManager $keys, SettingsManager $settings, #[\SensitiveParameter] array $request): array {
    $id = $request['connection_id'];
    $connection = null;
    foreach ($settings->listConnections() as $candidate) {
        if ($candidate['connection_id'] === $id) { $connection = $candidate; break; }
    }
    if ($connection === null) return ['outcome'=>'missing','data'=>null];
    $view = ['connection_id'=>$id,'revision'=>$connection['revision'],'configured'=>$connection['api_key_set'],'api_key_id'=>$connection['api_key_id']];
    if ($connection['revision'] !== $request['revision']) return ['outcome'=>'conflict','data'=>$view];
    $secret = $request['secret'] ?? null;
    if (!is_string($secret)) throw new InvalidArgumentException();
    if (trim($secret) === '') return ['outcome'=>'unchanged','data'=>$view];
    $origin = SettingsRegistry::endpointOrigin((string)$connection['endpoint']);
    if ($origin === null) return ['outcome'=>'invalid_state','data'=>$view];
    $provider = null;
    foreach (ApiKeyManager::PROVIDERS as $name=>$definition) {
        if ($definition['kind'] === $connection['kind'] && $definition['origin'] === $origin) { $provider=$name; break; }
    }
    if ($provider === null && $connection['kind'] === 'llm') $provider='custom_openai';
    if ($provider === null) return ['outcome'=>'provider_mismatch','data'=>$view];
    $created = $keys->createKey($provider, 'Connection '.$id, $provider === 'custom_openai' ? $origin : null, trim($secret));
    if ($created['outcome'] !== 'created') return $created;
    $newId = $created['data']['key_id'];
    $bound = $settings->updateConnection($id, $request['revision'], ['api_key_id'=>$newId]);
    if (!in_array($bound['outcome'], ['updated','unchanged'], true)) {
        // A known revision conflict never owns the new key. The normal usage guard protects concurrent users.
        $keys->deleteKey($newId, 1, fn(array $row):bool=>$settings->keyInUse($row));
        return $bound;
    }
    return ['outcome'=>'updated','data'=>['connection_id'=>$id,'revision'=>$bound['data']['revision'],'configured'=>true,'api_key_id'=>$newId]];
}

// Return masked metadata unless the user explicitly requests this root-local reveal.
function displayedKey(string $secret, bool $reveal): array {
    $chars=preg_split('//u',$secret,-1,PREG_SPLIT_NO_EMPTY);
    $preview=$secret==='' ? '' : ($chars===false||count($chars)<=8 ? '********' : implode('',array_slice($chars,0,4)).'********'.implode('',array_slice($chars,-4)));
    return ['preview'=>$preview,'configured'=>$secret!=='']+($reveal ? ['secret'=>$secret] : []);
}
function readStoredKey(ApiKeyManager $keys, string $id, bool $reveal): array {
    $meta=$keys->getKey($id);
    if($meta['outcome']!=='found') return ['outcome'=>'missing','data'=>null];
    $row=$meta['data']; $provider=ApiKeyManager::PROVIDERS[$row['provider']]??null;
    if($provider===null) return ['outcome'=>'unavailable','data'=>null];
    $resolved=$keys->resolveSecret($id,$provider['kind'],$row['origin']);
    if($resolved['outcome']!=='resolved') return ['outcome'=>'unavailable','data'=>null];
    return ['outcome'=>$reveal?'revealed':'previewed','data'=>displayedKey($resolved['secret'],$reveal)];
}
function readConnectionKey(ApiKeyManager $keys, array $row, bool $reveal): array {
    $id=SettingsRegistry::referencedKeyId($row['credential_ref']);
    if($id!==null) {
        $resolved=$keys->resolveSecret($id,$row['kind'],SettingsRegistry::endpointOrigin($row['endpoint'])??'');
        if($resolved['outcome']!=='resolved') return ['outcome'=>'unavailable','data'=>null];
        $secret=$resolved['secret'];
    } else {
        if($row['credential_ref']!==null && !SettingsRegistry::credentialBindingValid($row['kind'],$row['credential_ref'],$row['credential_origin'],$row['endpoint'])) return ['outcome'=>'unavailable','data'=>null];
        $secret=$row['credential_ref']===null ? '' : (\ChimServer\Core\SettingsManager\SettingsExecution::environmentCredential($row['credential_ref'])??'');
    }
    return ['outcome'=>$reveal?'revealed':'previewed','data'=>displayedKey($secret,$reveal)];
}

if (PHP_SAPI !== 'cli' || count($argv) !== 3) {
    exit(1);
}

try {
    require $argv[1] . '/Config/Autoloader.php';
    Autoloader::register();
    $raw = stream_get_contents(STDIN, 131073);
    if (strlen($raw) > 131072) {
        throw new InvalidArgumentException();
    }
    $request = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($request) || (array_is_list($request) && $request !== [])) {
        throw new InvalidArgumentException();
    }
    $keys = ApiKeyManager::fromEnvironment();
    $settings = new SettingsManager(DBManager::fromEnvironment(), $keys);
    $service = new ApiKeysService($keys, $settings);
    $operation = $argv[2];
    $fields = match ($operation) {
        'status', 'global-status' => [],
        'global-save' => ['key_id','revision','provider','label','origin','secret'],
        'global-reveal', 'global-test' => ['key_id','revision'],
        'create' => ['provider', 'label', 'origin', 'secret'],
        'edit' => ['key_id', 'revision', 'label', 'secret'],
        'test' => ['key_id'],
        'delete' => ['key_id', 'revision'],
        'bind' => ['connection_id', 'revision', 'key_id'],
        'import-environment' => ['connection_id', 'provider'],
        'guard-ready' => [],
        'save-connection' => ['connection_id','revision','secret'],
        'reveal-connection' => ['connection_id','revision'],
        'read-key' => ['key_id','reveal'],
        'import-legacy' => ['product', 'table', 'id', 'provider', 'label', 'origin', 'expected_key_id', '_legacy'],
        'bind-legacy' => ['product', 'table', 'id', 'key_id', 'expected_key_id', '_legacy'],
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
    if ($operation === 'global-test') {
        if(!isset($request['key_id'],$request['revision'])) throw new InvalidArgumentException();
        $meta=$keys->getKey($request['key_id']);
        if($meta['outcome']!=='found') $result=$meta;
        elseif($meta['data']['revision']!==$request['revision']) $result=['outcome'=>'conflict','data'=>null];
        elseif(ApiKeyCheck::checkUrl($meta['data']['provider'],$meta['data']['origin'])===null)
            $result=['outcome'=>'tested','data'=>['result'=>'unsupported','message'=>'No quick check is available for this provider and origin.']];
        else $result=$service->testKey($request['key_id']);
    } elseif ($operation === 'global-save') {
        if(isset($request['key_id'])) {
            if(!isset($request['revision'])||array_diff(array_keys($request),['key_id','revision','label','secret'])!==[]) throw new InvalidArgumentException();
            $changes=array_intersect_key($request,array_flip(['label','secret']));
            $current=$keys->getKey($request['key_id']);
            if($current['outcome']==='found' && ($changes['label']??null)===$current['data']['label']) unset($changes['label']);
            $result=$service->updateKey($request['key_id'],$request['revision'],$changes);
        } else {
            if(array_diff(array_keys($request),['provider','label','origin','secret'])!==[]) throw new InvalidArgumentException();
            $result=$service->createKey($request);
        }
        if(in_array($result['outcome'],['created','updated','unchanged'],true) && $result['data']!==null) {
            $preview=readStoredKey($keys,$result['data']['key_id'],false);
            $result['data']+=($preview['data']??['preview'=>'','configured'=>false]);
            $result['data']['test_supported']=ApiKeyCheck::checkUrl($result['data']['provider'],$result['data']['origin'])!==null;
        }
    } elseif ($operation === 'global-reveal') {
        if(!isset($request['key_id'],$request['revision'])) throw new InvalidArgumentException();
        $meta=$keys->getKey($request['key_id']);
        $result=$meta['outcome']!=='found' ? $meta : ($meta['data']['revision']!==$request['revision']
            ? ['outcome'=>'conflict','data'=>null] : readStoredKey($keys,$request['key_id'],true));
    } elseif ($operation === 'global-status') {
        $result=$service->listKeys();
        if($result['outcome']==='listed') {
            foreach($result['data']['keys'] as &$key) {
                $preview=readStoredKey($keys,$key['key_id'],false);
                $key+=($preview['data']??['preview'=>'','configured'=>false]);
                $key['test_supported']=ApiKeyCheck::checkUrl($key['provider'],$key['origin'])!==null;
            }
            unset($key);
            foreach($result['data']['providers'] as $name=>&$definition) {
                $definition['test_supported']=ApiKeyCheck::checkUrl($name,$definition['origin']??'')!==null || in_array($name,['replicate','deepl','custom_openai'],true);
            }
            unset($definition);
        }
    } elseif ($operation === 'read-key') {
        if(!is_string($request['key_id']??null)||!is_bool($request['reveal']??null)) throw new InvalidArgumentException();
        $result=readStoredKey($keys,$request['key_id'],$request['reveal']);
    } elseif ($operation === 'reveal-connection') {
        if(!isset($request['connection_id'],$request['revision'])) throw new InvalidArgumentException();
        $result=['outcome'=>'missing','data'=>null];
        foreach(DBManager::fromEnvironment()->listProviderConnections() as $row) {
            if((int)$row['connection_id']!==$request['connection_id']) continue;
            $result=(int)$row['revision']!==$request['revision'] ? ['outcome'=>'conflict','data'=>null] : readConnectionKey($keys,$row,true);
            break;
        }
    } elseif ($operation === 'save-connection') {
        $result = saveConnectionKey($keys, $settings, $request);
    } elseif ($operation === 'guard-ready') {
        $keys->externalUsage();
        $result = ['outcome' => 'guard_ready', 'data' => null];
    } elseif ($operation === 'status') {
        $result = $service->listKeys();
        if ($result['outcome'] === 'listed') {
            $result['data']['connections'] = array_map(static fn (array $row): array => array_intersect_key($row,
                array_flip(['connection_id', 'label', 'kind', 'status', 'revision', 'api_key_id', 'credential_source'])), $settings->listConnections());
            $rawConnections=DBManager::fromEnvironment()->listProviderConnections();
            foreach($result['data']['connections'] as &$connection) {
                foreach($rawConnections as $rawRow) {
                    if((int)$rawRow['connection_id']===$connection['connection_id']) {
                        $preview=readConnectionKey($keys,$rawRow,false);
                        $connection['preview']=$preview['data']['preview']??'';
                        break;
                    }
                }
            }
            unset($connection);
            $result['data']['warnings'] = ['Legacy HerikaServer, STOBE and Dialectic keys and assignments are preserved. They are not silently imported, merged or synchronized.'];
        }
    } elseif (in_array($operation, ['import-legacy', 'bind-legacy'], true)) {
        require_once __DIR__ . '/legacy_keys.php';
        $legacy = new DistroLegacyKeys($keys, $request['_legacy']);
        $result = $operation === 'import-legacy' ? $legacy->importKey($request) : $legacy->bind($request['key_id'] ?? null);
    } else {
        $result = match ($operation) {
            'create' => $service->createKey($request),
            'edit' => $service->updateKey($request['key_id'], $request['revision'], array_intersect_key($request, array_flip(['label', 'secret']))),
            'test' => $service->testKey($request['key_id']),
            // The manager checks CHIM 4 and every registered legacy consumer before deleting.
            'delete' => $service->deleteKey($request['key_id'], $request['revision']),
            'bind' => $settings->updateConnection($request['connection_id'], $request['revision'], ['api_key_id' => $request['key_id'] ?? null]),
            'import-environment' => $settings->importConnectionCredential($request['connection_id'], $request['provider']),
        };
    }
    $success = in_array($result['outcome'], ['listed', 'created', 'updated', 'unchanged', 'deleted', 'tested', 'imported', 'already_imported',
        'guard_ready', 'bound', 'detached', 'revealed', 'previewed'], true);
    // Public manager views only. An interrupted import may leave an unused key; retain its recovery ID.
    echo json_encode(['ok' => $success] + $result, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
} catch (Throwable) {
    echo json_encode(['ok' => false, 'outcome' => 'unavailable', 'error' => 'Shared key service is unavailable. Reload before retrying; an interrupted write may have completed.']) . "\n";
    exit(1);
}
