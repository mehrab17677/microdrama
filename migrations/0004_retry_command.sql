-- A person can retry a failed production from the failed step (graph command kind 'retry').
alter table graph_commands drop constraint if exists graph_commands_kind_check;
alter table graph_commands add constraint graph_commands_kind_check check (kind in ('start','resume','retry'));
