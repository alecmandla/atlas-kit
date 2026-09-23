<%*
// Weekly review. One note per ISO week under the areas folder's Weekly-Reviews/
// subfolder, named by the week (2026-W21). The weekly skill renders this template
// itself: it drops this block, fills the <% %> interpolations, and regenerates every
// section except "Next week's focus" and "Notes / reflections", which it preserves
// across re-runs. Use the template by hand for a week the skill did not run.
const today = tp.date.now("YYYY-MM-DD");
const isoWeek = moment(today).format("GGGG-[W]WW");
const monday = moment(today).isoWeekday(1).format("YYYY-MM-DD");
const sunday = moment(today).isoWeekday(7).format("YYYY-MM-DD");
if (tp.file.title !== isoWeek) { await tp.file.rename(isoWeek); }
-%>
---
date: <% today %>
type: weekly-review
iso_week: <% isoWeek %>
week_start: <% monday %>
week_end: <% sunday %>
tags: [weekly-review]
---

# Weekly Review — <% isoWeek %> (<% monday %> → <% sunday %>)

> **Needs attention this week**
>
> 1. 
> 2. 
> 3. 

## What got done this week

- 

### Shipped tasks

```dataview
TASK
WHERE completed AND completion >= date("<% monday %>") AND completion <= date("<% sunday %>")
```

## What's blocked or stuck

- 

### Stalled threads (≥ 14 days)

- 

## Needs attention

- [ ] 

## This week's meetings (you attended)

```dataview
TABLE date, title, project
FROM #meeting
WHERE date >= date("<% monday %>") AND date <= date("<% sunday %>")
SORT date ASC
```

## Active threads (new activity this week)

- 

## Open action items

```dataview
TASK
WHERE !completed AND due
SORT due ASC
LIMIT 25
```

## Next week's focus

*(Hand-edit this section. The weekly skill preserves it across re-runs.)*

1. 
2. 
3. 

## Notes / reflections

*(Hand-edit. Preserved across re-runs.)*

- 
