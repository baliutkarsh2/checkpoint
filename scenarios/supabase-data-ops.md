# Supabase — e-commerce data operations

## Setup

A Supabase database seeded with an e-commerce schema: `products` table
(4 products, one out-of-stock), `orders` table (2 orders), and `customers`
table (2 customers). Storage buckets: `product-images` (public) and
`invoices` (private). Use the `ecommerce` seed.

## Prompt

You are an admin managing the e-commerce Supabase project. Complete the
following tasks:

1. Query the `products` table and find all products where `stock` equals 0.
2. Update those out-of-stock products to set `active` = false.
3. Insert a new product into the `products` table:
   - name: "Mouse Pad XL"
   - price: 19.99
   - stock: 200
   - category: "accessories"
   - active: true
4. Create a new storage bucket named "receipts" (private, not public).
5. In your final answer, confirm the out-of-stock product name(s) you
   deactivated and the new product you inserted.

## Success Criteria

- [D] At least 1 bucket exists named "receipts"
- [P] The out-of-stock product (Mechanical Keyboard) has active=false
- [P] A new product named "Mouse Pad XL" exists in the products table
- [P] The receipts bucket is private (public=false)
- [P] The final answer names the deactivated product(s) and the new product

## Config

clones: supabase
seed: ecommerce
runs: 1
timeout: 90
tags: data-ops, supabase
