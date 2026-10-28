-- Add opt-in shared UUID references without replacing legacy rows or copying/clearing credentials.
-- Core runs this transaction as owner. The shared runtime role can inspect references and use a bounded CAS only.
DO $setup$
DECLARE target text; display_column text; existing oid;
BEGIN
    -- Hardened legacy databases may revoke public schema access; grant metadata access without row-wide reads.
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO __ROLE__', current_database());
    GRANT USAGE ON SCHEMA public TO __ROLE__;
    existing := to_regprocedure('public.ddistro_bind_shared_key(text,integer,text,uuid,uuid,oid)');
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname='ddistro_bind_shared_key'
        AND (p.oid IS DISTINCT FROM existing OR p.proowner<>(SELECT oid FROM pg_roles WHERE rolname=current_user)
            OR obj_description(p.oid,'pg_proc') IS DISTINCT FROM 'DwemerDistro shared key CAS v1')) THEN
        RAISE EXCEPTION 'Unknown shared key function; existing schema is preserved';
    END IF;
    FOREACH target IN ARRAY ARRAY['core_api_badge','core_llm_connector','core_tts_connector'] LOOP
        IF to_regclass('public.' || target) IS NULL THEN CONTINUE; END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid=to_regclass('public.' || target) AND relkind='r')
            OR NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                AND table_name=target AND column_name='id' AND data_type='integer')
            OR NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                AND table_name=target AND column_name IN ('label','name') AND data_type IN ('text','character varying'))
            OR EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                AND table_name=target AND column_name='shared_key_id' AND (data_type<>'uuid' OR is_nullable<>'YES')) THEN
            RAISE EXCEPTION 'Unsupported legacy key schema; existing settings are preserved';
        END IF;
        EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS shared_key_id uuid', target);
        SELECT CASE WHEN EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public'
            AND table_name=target AND column_name='label') THEN 'label' ELSE 'name' END INTO display_column;
        EXECUTE format('GRANT SELECT(id,%I,shared_key_id) ON public.%I TO __ROLE__', display_column,target);
    END LOOP;
END
$setup$;

CREATE OR REPLACE FUNCTION public.ddistro_bind_shared_key(
    p_target text, p_id integer, p_version text, p_prior uuid, p_new uuid, p_table oid
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog SET timezone = 'UTC' AS $body$
DECLARE affected bigint;
BEGIN
    IF p_target NOT IN ('core_api_badge','core_llm_connector','core_tts_connector')
        OR to_regclass('public.' || p_target)::oid IS DISTINCT FROM p_table THEN RETURN false; END IF;
    -- The snapshot includes all prior settings. Any concurrent edit, restore or reassignment refuses the write.
    EXECUTE format('UPDATE public.%I AS t SET shared_key_id=$1 WHERE id=$2
        AND encode(sha256(convert_to((to_jsonb(t)-''shared_key_id'')::text,''UTF8'')),''hex'')=$3
        AND shared_key_id IS NOT DISTINCT FROM $4',p_target)
        USING p_new,p_id,p_version,p_prior;
    GET DIAGNOSTICS affected=ROW_COUNT;
    RETURN affected=1;
END
$body$;
COMMENT ON FUNCTION public.ddistro_bind_shared_key(text,integer,text,uuid,uuid,oid) IS 'DwemerDistro shared key CAS v1';
REVOKE ALL ON FUNCTION public.ddistro_bind_shared_key(text,integer,text,uuid,uuid,oid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.ddistro_bind_shared_key(text,integer,text,uuid,uuid,oid) TO __ROLE__;
