-- Phase 11: ad placement. A brand colour for full-frame inserts, and the product insert placement kind.
alter table brand_briefs add column if not exists brand_color text not null default '#FFFFFF';
alter table placements drop constraint if exists placements_kind_check;
alter table placements add constraint placements_kind_check check (kind in ('phone_full','product_full','phone_in_hand','signage','spoken'));
