<%*
// Daily note. Filename is the date (YYYY-MM-DD); Periodic Notes or the core Daily
// notes plugin creates it. The morning and nightly skills append their report sections
// under the headings below and never touch anything else in this file.
const d = tp.date.now("YYYY-MM-DD", 0, tp.file.title, "YYYY-MM-DD");
-%>
---
type: daily
date: <% d %>
tags: [daily]
---

# <% tp.date.now("dddd, MMMM D, YYYY", 0, tp.file.title, "YYYY-MM-DD") %>

[[<% tp.date.now("YYYY-MM-DD", -1, tp.file.title, "YYYY-MM-DD") %>|yesterday]] · [[<% tp.date.now("YYYY-MM-DD", 1, tp.file.title, "YYYY-MM-DD") %>|tomorrow]]

## Focus

- 

## Notes

- 

## Tasks

- [ ] 

## Atlas morning report

*Written by the morning skill on scheduled or manual runs. Leave the heading in place.*

## Atlas nightly report

*Written by the nightly skill. Leave the heading in place.*
