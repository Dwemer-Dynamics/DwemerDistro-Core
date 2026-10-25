<?php

// Root launches this fixed owner-side adapter for CHIM's native provider-key rows. Full readback is restricted to the explicit root-local reveal operation.
declare(strict_types=1);
ini_set('display_errors', '0');
ini_set('log_errors', '0');

// Partial previews never expose a complete short key.
function keyPreview(string $secret): string {
    if ($secret === '') return '';
    $chars = preg_split('//u', $secret, -1, PREG_SPLIT_NO_EMPTY);
    if ($chars === false || count($chars) <= 8) return '********';
    return implode('', array_slice($chars,0,4)).'********'.implode('',array_slice($chars,-4));
}
function metadata(array $row): array {
    return ['id' => (int)$row['id'], 'label' => $row['label'], 'configured' => $row['configured'] === 't',
        'key_id' => $row['key_id'], 'version' => $row['version'], 'preview' => keyPreview($row['api_key'] ?? '')];
}

try {
    if (PHP_SAPI !== 'cli' || count($argv) !== 3 || !in_array($argv[2], ['status', 'save', 'create', 'reveal'], true)
        || preg_match('/^[A-Za-z_][A-Za-z0-9_]{0,62}$/D', $argv[1]) !== 1) throw new InvalidArgumentException();
    $raw = stream_get_contents(STDIN, 16385);
    if (strlen($raw) > 16384) throw new InvalidArgumentException();
    $request = json_decode($raw, true, 16, JSON_THROW_ON_ERROR);
    if (!is_array($request) || array_diff(array_keys($request), match ($argv[2]) {
        'status' => [], 'reveal' => ['id','version'], 'save' => ['id','version','secret'], 'create' => ['label','secret']
    }) !== []) throw new InvalidArgumentException();
    $db = @pg_connect("host=/var/run/postgresql port=5432 dbname='" . $argv[1] . "' user=postgres", PGSQL_CONNECT_FORCE_NEW);
    if ($db === false) throw new RuntimeException();
    // Keys must not enter query/error parameter logs. These settings last only for this helper connection.
    if (@pg_query($db, "SET log_statement='none'; SET log_min_error_statement='panic'; SET log_parameter_max_length=0; SET log_parameter_max_length_on_error=0;") === false) throw new RuntimeException();
    $columns = "id,label,api_key,(COALESCE(api_key,'')<>'' OR to_jsonb(b)->>'shared_key_id' IS NOT NULL) AS configured,
        to_jsonb(b)->>'shared_key_id' AS key_id,b.xmin::text||':'||b.tableoid::text AS version";
    if ($argv[2] === 'status') {
        $result = @pg_query($db, 'SELECT ' . $columns . ' FROM public.core_api_badge b ORDER BY id LIMIT 129');
        if ($result === false || pg_num_rows($result) > 128) throw new RuntimeException();
        $response = ['ok'=>true,'outcome'=>'listed','data'=>['rows'=>array_map('metadata',pg_fetch_all($result))]];
    } elseif ($argv[2] === 'reveal') {
        $id=$request['id']??null; $version=$request['version']??null;
        if(!is_int($id)||$id<1||!is_string($version)) throw new InvalidArgumentException();
        $result=@pg_query_params($db,'SELECT '.$columns.' FROM public.core_api_badge b WHERE id=$1',[$id]);
        if($result===false||pg_num_rows($result)!==1) throw new RuntimeException();
        $record=pg_fetch_assoc($result); $current=metadata($record);
        $response=$current['version']!==$version
            ? ['ok'=>false,'outcome'=>'conflict','error'=>'This key changed. Reload before showing it.']
            : ['ok'=>true,'outcome'=>'revealed','data'=>$current+['secret'=>$record['api_key']??'']];
    } else {
        $secret = $request['secret'] ?? null;
        if (!is_string($secret) || str_contains($secret,"\0") || strlen($secret)>4096) throw new InvalidArgumentException();
        $secret = trim($secret);
        if (@pg_query($db,'BEGIN; SET LOCAL lock_timeout=2000; SET LOCAL statement_timeout=10000;') === false) throw new RuntimeException();
        if ($argv[2] === 'create') {
            $label = $request['label'] ?? null;
            if (!is_string($label) || preg_match('/^[^\x00-\x1F\x7F]{1,64}$/uD', trim($label))!==1 || $secret==='') throw new InvalidArgumentException();
            $label = trim($label);
            if (@pg_query($db,'LOCK TABLE public.core_api_badge IN SHARE ROW EXCLUSIVE MODE')===false) throw new RuntimeException();
            $existing = @pg_query_params($db,'SELECT id FROM public.core_api_badge WHERE LOWER(label)=LOWER($1)',[$label]);
            if ($existing === false) throw new RuntimeException();
            if (pg_num_rows($existing)>0) {
                $response=['ok'=>false,'outcome'=>'conflict','error'=>'A provider with this name already exists. Edit its row instead.'];
            } elseif ((int)pg_fetch_result(pg_query($db,'SELECT count(*) FROM public.core_api_badge'),0,0)>=128) {
                $response=['ok'=>false,'outcome'=>'limit_reached','error'=>'This server already has 128 provider key rows. No new row was added.'];
            } else {
                $result=@pg_query_params($db,'INSERT INTO public.core_api_badge AS b(label,api_key) VALUES($1,$2) RETURNING '.$columns,[$label,$secret]);
                if($result===false) throw new RuntimeException();
                $response=['ok'=>true,'outcome'=>'created','data'=>metadata(pg_fetch_assoc($result))];
            }
        } else {
            $id=$request['id']??null; $version=$request['version']??null;
            if(!is_int($id)||$id<1||!is_string($version)||preg_match('/^[0-9]+:[0-9]+$/D',$version)!==1) throw new InvalidArgumentException();
            $result=@pg_query_params($db,'SELECT '.$columns.' FROM public.core_api_badge b WHERE id=$1 FOR UPDATE',[$id]);
            if($result===false||pg_num_rows($result)!==1) throw new RuntimeException();
            $current=metadata(pg_fetch_assoc($result));
            if($current['version']!==$version) {
                $response=['ok'=>false,'outcome'=>'conflict','error'=>'This provider changed. Reload its current row before saving.','data'=>$current];
            } elseif($secret==='') {
                $response=['ok'=>true,'outcome'=>'unchanged','data'=>$current];
            } else {
                $hasShared=@pg_query($db,"SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='core_api_badge' AND column_name='shared_key_id'");
                if($hasShared===false) throw new RuntimeException();
                // Match CHIM's native edit semantics: replace this badge, leaving other saved shared keys untouched.
                $clear=pg_num_rows($hasShared)>0?',shared_key_id=NULL':'';
                $result=@pg_query_params($db,'UPDATE public.core_api_badge AS b SET api_key=$1'.$clear.' WHERE id=$2 RETURNING '.$columns,[$secret,$id]);
                if($result===false||pg_num_rows($result)!==1) throw new RuntimeException();
                $response=['ok'=>true,'outcome'=>'updated','data'=>metadata(pg_fetch_assoc($result))];
            }
        }
        if(@pg_query($db,'COMMIT')===false) throw new RuntimeException();
    }
    echo json_encode($response, JSON_THROW_ON_ERROR|JSON_UNESCAPED_SLASHES)."\n";
} catch (InvalidArgumentException) {
    echo json_encode(['ok'=>false,'outcome'=>'invalid','error'=>'Enter a provider name and a valid key (up to 4096 bytes). Existing keys are unchanged.'])."\n"; exit(1);
} catch (Throwable) {
    echo json_encode(['ok'=>false,'outcome'=>'unavailable','error'=>'Provider key save is unavailable. Reload before retrying; the write may have completed.'])."\n"; exit(1);
}
