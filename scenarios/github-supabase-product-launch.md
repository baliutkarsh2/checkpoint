# GitHub + Supabase — product launch coordination

## Setup

A GitHub workspace with the `acme/webapp` repository and no open issues.
A Supabase database with the e-commerce schema: `products` table with 4
existing products (one out-of-stock). Use `seed: github=small-project, supabase=ecommerce`.

## Prompt

A new product is ready to ship: "Mouse Pad XL" (price $24.99, stock 150,
category "accessories"). You need to coordinate the launch across two systems:

1. Open a GitHub issue in `acme/webapp` titled exactly
   "Product Launch: Mouse Pad XL" with a body that describes the product
   and marks it as a launch tracking ticket.

2. Insert the product into the Supabase `products` table with these fields:
   - name: "Mouse Pad XL"
   - price: 24.99
   - stock: 150
   - category: "accessories"
   - active: true

In your final answer, state the GitHub issue number you created and confirm
that the Supabase product was inserted.

## Success Criteria

- [D] An issue titled "Product Launch: Mouse Pad XL" exists
- [D] The issue is in the open state
- [P] A product named "Mouse Pad XL" exists in the Supabase products table with active=true
- [P] The agent's final answer references the GitHub issue number and confirms the Supabase insertion

## Config

clones: github, supabase
seed: github=small-project, supabase=ecommerce
runs: 1
timeout: 90
tags: multi-clone, github, supabase, product-launch
