<%*
// Person note. Lives under the CRM folder; Templater's folder template applies this
// automatically to new files there. The people-extract skill creates and refreshes
// these too, and only ever updates `mention_count` and `last_seen`; everything you
// write below the frontmatter is preserved.
const name = tp.file.title.replace(/-/g, " ");
-%>
---
type: person
name: "<% name %>"
emails: []
organization: 
role: 
tier: contact
mention_count: 0
last_seen: 
tags: [person]
---

# <% name %>

**Organization:** 
**Role:** 
**How we know each other:** 

## Context

- 

## Recent meetings

```dataview
TABLE date, title
FROM #meeting
WHERE contains(participants, this.file.link) OR contains(file.outlinks, this.file.link)
SORT date DESC
LIMIT 10
```

## Notes

- 
