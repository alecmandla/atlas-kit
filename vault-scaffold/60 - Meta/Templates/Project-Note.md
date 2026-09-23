<%*
// Project note. Applied by Templater's folder template to new files under the projects
// folder. A project is work with an end; when it ends, move the folder to the archive
// folder by hand (no generated skill moves notes). The `thread` field is the slug the
// synthesis skill uses; leave it empty until a thread exists for this project.
const name = tp.file.title;
const today = tp.date.now("YYYY-MM-DD");
-%>
---
type: project
title: "<% name %>"
status: active
started: <% today %>
client: 
area: 
thread: 
tags: [project]
---

# <% name %>

**Status:** active since <% today %>
**Client or area:** 
**Outcome we want:** 

## Goal

- 

## Open tasks

```dataview
TASK
FROM "<% tp.file.folder(true) %>"
WHERE !completed
SORT due ASC
```

## Recent meetings

```dataview
TABLE date, attendees
FROM #meeting
WHERE project = this.file.name OR contains(file.outlinks, this.file.link)
SORT date DESC
LIMIT 10
```

## Decisions

- 

## Log

- <% today %> — created
