SELECT COUNT(*) AS zero_balance_users
FROM users
WHERE used_requests = 0
  AND free_requests = 0
  AND paid_requests = 0;
