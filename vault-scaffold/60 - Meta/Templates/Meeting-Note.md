<%*
// Meeting note. The meetings ingest writes notes in this shape automatically; use the
// template by hand for meetings that were not recorded. Keep the frontmatter keys: the
// entity and people layers read `participants` ([[First-Last]] wikilinks), `attendee_emails`
// (the raw addresses the people extractor resolves), `client`, and `date`.
const title = await tp.system.prompt("Meeting title");
const date = tp.date.now("YYYY-MM-DD");
await tp.file.rename(date + " " + title);
-%>
---
type: meeting
date: <% date %>
title: "<% title %>"
participants: []
attendee_emails: []
client: 
project: 
meeting_id: 
transcript_url: 
tags: [meeting]
---

# <% title %>

**Date:** <% date %>
**Participants:** 

## Summary

- 

## Decisions

- 

## Action items

- [ ] 

## Full transcript

*Not stored in the vault. If this meeting was recorded, the ingest sets `transcript_url`
above and the research skill fetches the transcript on demand.*
