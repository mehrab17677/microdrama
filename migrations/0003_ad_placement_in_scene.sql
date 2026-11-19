-- Ad placement redo: the brand appears INSIDE generated scenes (docs/AD_PLACEMENT.md).
alter table brand_briefs add column if not exists product_description text not null default '';
alter table brand_briefs add column if not exists product_physical text not null default '';
alter table brand_briefs add column if not exists label_texts text[] not null default '{}';
alter table brand_briefs add column if not exists forbidden_claims text[] not null default '{}';
alter table brand_briefs add column if not exists min_exposure_seconds numeric not null default 3;
alter table brand_briefs add column if not exists product_sheet jsonb;
alter table brand_assets add column if not exists view text not null default 'front';
update placements set kind = 'product_in_use' where kind in ('phone_full', 'product_full', 'phone_in_hand');
update placements set kind = 'logo_in_scene' where kind = 'signage';
alter table placements drop constraint if exists placements_kind_check;
alter table placements add constraint placements_kind_check check (kind in
  ('product_in_use','product_shown','product_ambient','logo_in_scene','spoken'));
alter table placements drop constraint if exists placements_status_check;
alter table placements add constraint placements_status_check check (status in ('planned','placed','verified','failed','approved','rejected'));
alter table placements add column if not exists approved_by uuid references users(id);
alter table placements add column if not exists approved_at timestamptz;
alter table placements add column if not exists review_note text;
alter table placements add column if not exists exposure_seconds numeric;
alter table placements add column if not exists fidelity jsonb;
