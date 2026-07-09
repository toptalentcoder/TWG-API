-- Follow-up cleanup: retire the old POC key table.
-- RUN THIS ONLY AFTER the new property-search function version (the one that calls
-- validate_api_key) is deployed and verified live — the previous function version
-- authenticates against this table, so dropping it earlier breaks live requests.
drop table if exists public.api_keys;
