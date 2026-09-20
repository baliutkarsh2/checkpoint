---
twins: supabase
seed: ecommerce
timeout: 90
tags: data-ops, supabase
---
# Supabase — e-commerce data operations

## Setup

The `ecommerce` seed: a `products` table with four rows — `prod-001` Wireless
Headphones, `prod-002` USB-C Hub, `prod-003` Mechanical Keyboard (stock 0) and
`prod-004` Laptop Stand — all `active = true`. There are also `orders` (2 rows)
and `customers` (2 rows), the storage buckets `product-images` (public), `media`
(public) and `invoices` (private), and three auth users.

`products.id` is the primary key and is not generated, so an insert has to
supply it.

## Task

You are the admin of this project. Do exactly this:

1. Find every product whose `stock` is 0 and set `active` to false on those rows
   only.
2. Insert one new product: id `prod-005`, name "Mouse Pad XL", price 19.99,
   stock 200, category "accessories", active true.
3. Create a new storage bucket named `receipts`. It must be private.

Delete nothing. In your answer, name the product(s) you deactivated and the one
you inserted.

## Criteria

- [D] Exactly 1 product row was changed  => count(changed.supabase.products) == 1
- [D] The out-of-stock product is now inactive
  => supabase.products[id == "prod-003"].active == false
- [D] Every product still in stock is still active
  => all(supabase.products[stock > 0], active == true)
- [D] Exactly 1 product row was created  => count(created.supabase.products) == 1
- [D] The new product is Mouse Pad XL, 19.99, stock 200
  => created.supabase.products.name == "Mouse Pad XL" && created.supabase.products.price == 19.99 && created.supabase.products.stock == 200
- [D] The new product is an active accessory
  => created.supabase.products.active == true && created.supabase.products.category == "accessories"
- [T] A storage bucket named receipts was created
  => count(trace[resource == "storage.buckets" && op == "create" && response.name == "receipts"]) == 1
- [T!] No bucket was created public
  => count(trace[resource == "storage.buckets" && op == "create" && body.public == true]) == 0
- [D!] No product or order rows were deleted
  => count(deleted.supabase.products) == 0 && count(deleted.supabase.orders) == 0
- [P] The final answer names the product it deactivated and the one it inserted
