# Structured automatic recall

`recall_candidates(query)` uses the existing MCP authentication boundary.
The query is trimmed and limited to 2,000 Unicode code points. Empty queries
return an empty result without searching. The tool uses the same ranked
retrieval and automatic surfacing policy as ordinary breath search, scoped to
active search results. It does not run exact-ID lookup, random surfacing,
rendering, reinforcement or webhooks.

The result is exclusively in MCP `structuredContent`. Schema version 1 has
`schema_version` and `candidates`, with up to six candidates. Each candidate
contains `bucket_id`, `content`, `score`, `created_at`, `updated_at`,
`domain`, `tags`, `importance`, `valence`, `arousal`, `policy` and
`relations`. Content is limited to 1,000 Unicode code points. The payload is
limited to 23 KiB under ASCII JSON encoding, reserving room in a 24 KiB
response budget for the protocol envelope. Unavailable scalar metadata is
null. Scores are for comparing candidates within a query.

Every returned candidate passed the automatic policy and has
`policy.silent_context=true`. Current records lack reliable authorization for
proactive mention or general-purpose correction edges, so
`policy.speakable=false`, `relations.corrects=[]` and
`relations.supersedes=[]`. Once content enters a model's context, silent
context is a behavioral instruction. It withholds permission to raise the
material proactively and cannot enforce information-flow isolation or
guarantee that the model will omit it from its response.
