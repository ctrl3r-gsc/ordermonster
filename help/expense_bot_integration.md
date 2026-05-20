# Expense Bot Integration

OrderMonster and Expense Bot share the same PostgreSQL database through the
`company_transactions` table.

Expense Bot should insert expense rows only. It does not need access to
`orders`, `order_items`, `order_payments`, `shops`, or `products`.

Required fields for an expense:

```json
{
  "type": "expense",
  "source_bot": "expense_bot",
  "category": "rent",
  "amount": 12000,
  "currency": "THB",
  "payment_method": "transfer",
  "description": "May office rent",
  "transaction_date": "2026-05-21T10:00:00+07:00"
}
```

Allowed values:

- `type`: `expense`
- `source_bot`: `expense_bot` or `manual`
- `payment_method`: `cash`, `transfer`, `crypto`, `unknown`
- `currency`: default is `THB`

Notes:

- `category` should be a stable lowercase key, for example `rent`,
  `salary`, `delivery`, `supplies`, `marketing`, `refund`, or `other`.
- `amount` must be a positive numeric value.
- `transaction_date` should be the real payment date, not the bot message date
  unless those are the same.
- `related_order_id` should stay `null` for Expense Bot expenses.
- OrderMonster writes income rows with `source_bot = ordermonster`,
  `type = income`, and `category = sales`.
