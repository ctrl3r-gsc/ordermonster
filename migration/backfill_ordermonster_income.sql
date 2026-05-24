INSERT INTO company_transactions (
    type,
    source_bot,
    category,
    amount,
    currency,
    payment_method,
    related_order_id,
    description,
    transaction_date,
    created_at,
    updated_at
)
SELECT
    'income'::company_transaction_type AS type,
    'ordermonster'::company_transaction_source_bot AS source_bot,
    'sales' AS category,
    SUM(op.amount)::numeric(12, 2) AS amount,
    'THB' AS currency,
    CASE latest_payment.payment_method
        WHEN 'cash' THEN 'cash'
        WHEN 'transaction' THEN 'transfer'
        WHEN 'crypto' THEN 'crypto'
        ELSE 'unknown'
    END::company_transaction_payment_method AS payment_method,
    o.id AS related_order_id,
    'Order #' || COALESCE(o.display_number, o.id)::text AS description,
    COALESCE(MAX(op.created_at), now()) AS transaction_date,
    now() AS created_at,
    now() AS updated_at
FROM orders o
JOIN order_payments op ON op.order_id = o.id
LEFT JOIN LATERAL (
    SELECT payment_method
    FROM order_payments latest_op
    WHERE latest_op.order_id = o.id
    ORDER BY latest_op.created_at DESC NULLS LAST, latest_op.id DESC
    LIMIT 1
) latest_payment ON TRUE
WHERE o.payment_status = 'paid'
GROUP BY o.id, latest_payment.payment_method
HAVING SUM(op.amount) > 0
ON CONFLICT (source_bot, related_order_id)
WHERE source_bot = 'ordermonster'
  AND type = 'income'
  AND related_order_id IS NOT NULL
DO UPDATE SET
    category = EXCLUDED.category,
    amount = EXCLUDED.amount,
    currency = EXCLUDED.currency,
    payment_method = EXCLUDED.payment_method,
    description = EXCLUDED.description,
    transaction_date = EXCLUDED.transaction_date,
    updated_at = now();
