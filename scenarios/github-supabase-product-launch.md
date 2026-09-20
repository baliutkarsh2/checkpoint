---
twins: github, supabase
seed: github=small-project, supabase=ecommerce
timeout: 120
tags: multi-clone, github, supabase, product-launch
---
# GitHub + Supabase — coordinate a product launch

## Setup

GitHub's `small-project` seed has `acme/webapp` with two open issues (#1 and
#2), so a new issue takes number 3.

Supabase's `ecommerce` seed has a `products` table with four rows (`prod-001` …
`prod-004`). `products.id` is the primary key and is not generated, so an insert
has to supply it.

## Task

"Mouse Pad XL" is ready to ship. Coordinate the launch across both systems:

1. Open an issue in `acme/webapp` titled exactly
   "Product Launch: Mouse Pad XL", with a body describing the product and
   saying this is the launch tracking ticket.
2. Insert the product into the Supabase `products` table: id `prod-005`, name
   "Mouse Pad XL", price 24.99, stock 150, category "accessories", active true.

Change nothing else in either system. End your answer with the GitHub issue
number written as `#N`, and confirm the Supabase insert.

## Criteria

- [D] Exactly 1 issue was created
- [D] An issue titled "Product Launch: Mouse Pad XL" exists
- [D] The new issue is open  => created.github.issues.state == "open"
- [D] The final answer quotes the new issue's number  => answer ~ /#3\b/
- [D] Exactly 1 product row was created  => count(created.supabase.products) == 1
- [D] The new product is Mouse Pad XL, 24.99, stock 150
  => created.supabase.products.name == "Mouse Pad XL" && created.supabase.products.price == 24.99 && created.supabase.products.stock == 150
- [D] The new product is an active accessory
  => created.supabase.products.active == true && created.supabase.products.category == "accessories"
- [D!] The four seeded products were left alone
  => count(changed.supabase.products) == 0 && count(deleted.supabase.products) == 0
- [D!] No issues were deleted
- [P] The final answer confirms both the GitHub issue and the Supabase insert
