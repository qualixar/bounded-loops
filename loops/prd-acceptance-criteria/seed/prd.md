# PRD: Course Checkout Flow

## Story: As a visitor, I want to buy the course with a credit card

A visitor lands on the course page, clicks "Buy now", and completes checkout
via Paddle without leaving a hosted checkout overlay.

### Acceptance Criteria
- [ ] Clicking "Buy now" opens the Paddle checkout overlay within 1 second.
- [ ] A successful payment redirects to the course access page.
- [ ] A failed payment shows an inline error and does not close the overlay.

## Story: As a customer, I want a receipt emailed to me

After a successful purchase, the customer should get an email receipt so
they have proof of purchase for expensing or taxes.

## Story: As an admin, I want to see all completed purchases in a dashboard

The admin dashboard lists every completed purchase with buyer email, amount,
and timestamp, sortable by date.

### Acceptance Criteria
- [ ] The dashboard lists every completed purchase with email, amount, and
      timestamp.
- [ ] The list is sortable by timestamp, newest first by default.
