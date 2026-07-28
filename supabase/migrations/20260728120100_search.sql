-- =====================================================================
-- HardhatsAI — retrieval layer
--
-- Ordering matters: hard structural filtering FIRST, semantic ranking
-- SECOND. An engineer asking for EI60 must never be shown an EI30 product
-- because the marketing copy embedded well.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Evaluate one structured requirement against one product.
--
-- Requirement shape:
--   {"key": "fire_resistance_ei",     "op": ">=",       "value": 60}
--   {"key": "thermal_conductivity",   "op": "<=",       "value": 0.035}
--   {"key": "reaction_to_fire",       "op": "class>=",  "value": "B"}
--   {"key": "ce_marked",              "op": "=",        "value": "true"}
--
-- Returns 'met' | 'unmet' | 'unknown'.
--
-- 'unknown' is a first-class outcome and is never silently treated as
-- failure: it means the source documentation is silent, which the UI must
-- show as a data gap rather than a rejection.
-- ---------------------------------------------------------------------
create or replace function public.spec_requirement_status(
  p_product_id uuid,
  p_req        jsonb
)
returns text
language plpgsql
stable
set search_path = public
as $$
declare
  v_def       spec_definitions%rowtype;
  v_spec      product_specs%rowtype;
  v_op        text := coalesce(p_req ->> 'op', '>=');
  v_txt       text := p_req ->> 'value';
  v_num       numeric;
  v_rank_req  int;
  v_rank_have int;
begin
  select * into v_def from spec_definitions where key = p_req ->> 'key';
  if not found then
    return 'unknown';
  end if;

  select * into v_spec
    from product_specs
   where product_id = p_product_id
     and spec_definition_id = v_def.id
     and is_current;

  if not found or v_spec.presence = 'not_stated' then
    return 'unknown';
  end if;

  begin
    v_num := v_txt::numeric;
  exception when others then
    v_num := null;
  end;

  case v_op
    when '>=' then
      if v_spec.value_numeric is null or v_num is null then return 'unknown'; end if;
      return case when v_spec.value_numeric >= v_num then 'met' else 'unmet' end;

    when '<=' then
      if v_spec.value_numeric is null or v_num is null then return 'unknown'; end if;
      -- For ranges, the worst case must satisfy the requirement.
      return case
        when coalesce(v_spec.value_numeric_max, v_spec.value_numeric) <= v_num
        then 'met' else 'unmet' end;

    when '=' then
      return case
        when v_num is not null and v_spec.value_numeric = v_num then 'met'
        when v_spec.value_text is not null and v_spec.value_text = v_txt then 'met'
        when v_spec.value_enum is not null and v_spec.value_enum = v_txt then 'met'
        when v_spec.value_bool is not null
             and v_spec.value_bool = (lower(v_txt) in ('true', 't', 'yes', 'ja')) then 'met'
        else 'unmet'
      end;

    when 'class>=' then
      -- enum_ordinals ranks BEST = HIGHEST (Euroclass A1 = 7 ... F = 1),
      -- so "at least B" is a plain >= on the ordinal.
      if v_spec.value_enum is null or v_def.enum_ordinals is null then return 'unknown'; end if;
      v_rank_req  := (v_def.enum_ordinals ->> v_txt)::int;
      v_rank_have := (v_def.enum_ordinals ->> v_spec.value_enum)::int;
      if v_rank_req is null or v_rank_have is null then return 'unknown'; end if;
      return case when v_rank_have >= v_rank_req then 'met' else 'unmet' end;

    else
      return 'unknown';
  end case;
end;
$$;

-- ---------------------------------------------------------------------
-- Hybrid product search.
--
-- Pipeline:
--   1. scope        : published products, optional category/supplier narrowing
--   2. hard filter  : every required spec evaluated; p_strict drops any
--                     product with an explicit 'unmet'
--   3. vector recall: cosine kNN over document chunks of surviving products
--   4. keyword recall: exact-token FTS (catches 'EI60', 'S200', article codes
--                     that embeddings routinely blur)
--   5. fusion       : reciprocal rank fusion + small trust bonus for suppliers
--                     with complete data
--
-- Returns the requirement breakdown alongside the score so the answer layer
-- can explain fit without a second round trip.
-- ---------------------------------------------------------------------
create or replace function public.search_products(
  p_query_embedding vector(1536),
  p_query_text      text    default null,
  p_category_id     uuid    default null,
  p_supplier_id     uuid    default null,
  p_required_specs  jsonb   default '[]'::jsonb,
  p_strict          boolean default true,
  p_limit           int     default 20,
  p_pool            int     default 300
)
returns table (
  product_id           uuid,
  score                double precision,
  vector_rank          int,
  keyword_rank         int,
  best_chunk_id        uuid,
  requirements_met     jsonb,
  requirements_unmet   jsonb,
  requirements_unknown jsonb
)
language plpgsql
set search_path = public
as $$
begin
  -- Post-filtering a plain HNSW scan silently loses recall once the hard
  -- filter is selective. Iterative scan keeps pulling until the limit is
  -- genuinely satisfied. Requires pgvector >= 0.8.
  perform set_config('hnsw.iterative_scan', 'relaxed_order', true);

  return query
  with req as (
    select value as r from jsonb_array_elements(p_required_specs)
  ),
  eligible as (
    select p.id, p.supplier_company_id
      from products p
     where p.status = 'published'
       and (p_category_id is null or p.category_id = p_category_id)
       and (p_supplier_id is null or p.supplier_company_id = p_supplier_id)
  ),
  evaluated as (
    select e.id as pid,
           r.r   as req,
           public.spec_requirement_status(e.id, r.r) as st
      from eligible e
      cross join req r
  ),
  graded as (
    select e.id as pid,
           coalesce(jsonb_agg(ev.req) filter (where ev.st = 'met'),     '[]'::jsonb) as met,
           coalesce(jsonb_agg(ev.req) filter (where ev.st = 'unmet'),   '[]'::jsonb) as unmet,
           coalesce(jsonb_agg(ev.req) filter (where ev.st = 'unknown'), '[]'::jsonb) as unknown
      from eligible e
      left join evaluated ev on ev.pid = e.id
     group by e.id
  ),
  kept as (
    select * from graded
     where not p_strict or jsonb_array_length(unmet) = 0
  ),
  vec_raw as (
    select pd.product_id as pid,
           dc.id         as chunk_id,
           dc.embedding <=> p_query_embedding as dist
      from document_chunks dc
      join product_documents pd on pd.document_id = dc.document_id
      join kept k               on k.pid = pd.product_id
     where dc.embedding is not null
     order by dc.embedding <=> p_query_embedding
     limit p_pool
  ),
  vec as (
    select pid, chunk_id, row_number() over (order by dist) as rnk from vec_raw
  ),
  vec_best as (
    select pid,
           min(rnk)                              as rnk,
           (array_agg(chunk_id order by rnk))[1] as chunk_id
      from vec group by pid
  ),
  kw_raw as (
    select pd.product_id as pid,
           ts_rank_cd(dc.tsv, websearch_to_tsquery('simple', p_query_text)) as r
      from document_chunks dc
      join product_documents pd on pd.document_id = dc.document_id
      join kept k               on k.pid = pd.product_id
     where p_query_text is not null
       and dc.tsv @@ websearch_to_tsquery('simple', p_query_text)
     order by r desc
     limit p_pool
  ),
  kw as (
    select pid, row_number() over (order by r desc) as rnk from kw_raw
  ),
  kw_best as (
    select pid, min(rnk) as rnk from kw group by pid
  )
  select
    k.pid,
    (
      coalesce(1.0 / (60 + v.rnk), 0)
      + coalesce(1.0 / (60 + w.rnk), 0)
      + 0.004 * sc.data_completeness
    )::double precision,
    v.rnk::int,
    w.rnk::int,
    v.chunk_id,
    k.met,
    k.unmet,
    k.unknown
  from kept k
  join eligible e            on e.id = k.pid
  join supplier_companies sc on sc.id = e.supplier_company_id
  left join vec_best v       on v.pid = k.pid
  left join kw_best  w       on w.pid = k.pid
  where v.pid is not null or w.pid is not null
  order by 2 desc
  limit p_limit;
end;
$$;

-- ---------------------------------------------------------------------
-- Retrieve grounding chunks for answer generation, scoped to the products
-- already selected by search_products. The generator is only ever allowed
-- to cite chunk ids returned here.
-- ---------------------------------------------------------------------
create or replace function public.match_chunks(
  p_query_embedding vector(1536),
  p_product_ids     uuid[],
  p_limit           int default 12
)
returns table (
  chunk_id     uuid,
  document_id  uuid,
  product_id   uuid,
  page_start   int,
  page_end     int,
  heading_path text[],
  content      text,
  similarity   double precision
)
language sql
stable
set search_path = public
as $$
  select dc.id,
         dc.document_id,
         pd.product_id,
         dc.page_start,
         dc.page_end,
         dc.heading_path,
         dc.content,
         (1 - (dc.embedding <=> p_query_embedding))::double precision
    from document_chunks dc
    join product_documents pd on pd.document_id = dc.document_id
   where pd.product_id = any (p_product_ids)
     and dc.embedding is not null
   order by dc.embedding <=> p_query_embedding
   limit p_limit;
$$;

-- ---------------------------------------------------------------------
-- Worker job claiming. SKIP LOCKED so multiple workers can run safely.
-- ---------------------------------------------------------------------
create or replace function public.claim_job(
  p_worker text,
  p_kinds  text[] default null
)
returns jobs
language plpgsql
set search_path = public
as $$
declare
  v_job jobs;
begin
  update jobs j
     set status    = 'running',
         attempts  = j.attempts + 1,
         locked_at = now(),
         locked_by = p_worker
   where j.id = (
     select id from jobs
      where status = 'pending'
        and run_after <= now()
        and (p_kinds is null or kind = any (p_kinds))
      order by priority, run_after
      for update skip locked
      limit 1
   )
  returning j.* into v_job;

  return v_job;
end;
$$;

-- ---------------------------------------------------------------------
-- Supersede a spec instead of updating it in place, preserving history.
-- ---------------------------------------------------------------------
create or replace function public.supersede_spec(
  p_old_spec_id uuid,
  p_new_spec_id uuid
)
returns void
language sql
set search_path = public
as $$
  update product_specs
     set is_current    = false,
         superseded_by = p_new_spec_id
   where id = p_old_spec_id;
$$;
