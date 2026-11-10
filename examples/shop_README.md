# The shop (M8d)

A tiny full-stack shop in newr: three products, a cart, a checkout.
It proves `db` plus `http` plus `ui` compose into a real app. It is
five files in `examples/`, named to avoid collision with the existing
`shop.newr` (the Discord shop bot) and `shop_items.newr` (its price
list).

## File map

- `shop_shared.newr`: the contract. `pub struct Product` (id, name,
  price, image_url), `pub struct CartLine` (product_id, qty, price),
  `pub struct CartRequest` (product_id, qty), and the route table as
  constants (`PRODUCTS_PATH`, `CART_PATH`, `ACTION_PATH`,
  `CHECKOUT_PATH`). No statements, no db, no http, no ui.
- `shop_view.newr`: the view builder. `pub fn shop_view(products,
  cart_total, query, buy, checkout) -> Widget` builds the tree: a
  column holding a title text, a search input, a list of product rows,
  a cart total text, and a Checkout button. Each product row is a row
  holding an image, a text, and a Buy button. Imported by both
  frontends.
- `shop_backend.newr`: `db` plus `http`. Opens `shop.db`, creates the
  products and cart tables, seeds three products (Tea 5000, Coffee
  7000, Cake 12000), serves `GET /products`, `POST /cart`,
  `GET /cart`, `POST /checkout`. No ui.
- `shop_frontend_js.newr`: the js server frontend. Fetches the
  products from the backend once at start, serves the rendered page
  on `GET /`, dispatches button taps and search changes on
  `GET /action`.
- `shop_frontend_py.newr`: the py native frontend. A native window
  through `ui.run`; Buy handlers post to the backend and the view
  re-reads the products and the cart from the backend on every
  invocation.

## Ports (conventions, not compiler facts)

- Backend: 8080.
- Js frontend: 8081.
- The py frontend is a window, it listens on no port.

## How to run the pair

Backend first, from an empty directory so `shop.db` starts fresh:

```
mkdir shop-run
cd shop-run
cp ../examples/shop_shared.newr ../examples/shop_backend.newr .
newr build shop_backend.newr --target py
python3 shop_backend.py
```

Then either frontend, in a second terminal (it needs the shared and
view files next to it):

```
mkdir shop-web
cd shop-web
cp ../examples/shop_shared.newr ../examples/shop_view.newr ../examples/shop_frontend_js.newr .
newr build shop_frontend_js.newr --target js
node shop_frontend_js.js
```

Open http://127.0.0.1:8081/ in a browser. Or run the py frontend
instead (`newr build shop_frontend_py.newr --target py`, then
`python3 shop_frontend_py.py`) for a native window.

## Button-index convention

The view builds buttons in a stable order: the Buy buttons in
product order (`data-action` 0, 1, 2), then Checkout last
(`data-action` 3). The search input is `data-input` 0. The js
frontend dispatches `/action` on that order with an if/else chain
(0 buys Tea, 1 buys Coffee, 2 buys Cake, 3 checks out). If the view
changes the button order, the dispatch must change with it. This is
a documented convention, not a compiler guarantee; the test
`tests/test_shop_view.py` renders the page and asserts the order
matches the dispatch table.

The `/action` route disambiguates by the `v` query value: a request
carrying `v` is a search input change (redirect to `/?q=<v>`); a
request without `v` is a button tap (`i` is the button index as a
string).

## Search behavior

- Js frontend: typing in the search box filters the product list
  through `/?q=`. A buy resets the filter: the redirect after a
  button tap goes to `/`, so the full list shows again.
- Py frontend: the search input is display-only. Its handler is a
  no-op and the query is always `""`, so the view always shows the
  full product list.

## Checkout

`POST /checkout` deletes all cart rows and returns an empty cart.
That is a demo simplification: a real shop would record an order
and charge for it. The cart total on the page is the sum of
`qty * price` over the cart lines.

## Which file builds for which target

The split is a project convention, documented here and in SPEC
section 22.4, not a compiler rule. Nothing enforces it; a user who
builds the backend for js gets a Node server that works, and a user
who builds a frontend for py gets a window that works. The intended
builds are: `shop_backend.newr` for py (any server target works,
the pair test uses py), `shop_frontend_js.newr` for js,
`shop_frontend_py.newr` for py, `shop_view.newr` and
`shop_shared.newr` for every target (they are imported, not run
alone).

## Recorded limitations (found while building the shop)

- Rows are built explicitly, not in a loop. The step-1 probe showed
  a single-expression int-returning lambda stored in a variable
  fails to compile on rs: the emitter types the `42` literal as i32
  against an i64 closure (`rustc` E0271). The block-body form and the
  none-returning form both work on rs, and the lambda-in-loop
  capture probe is green on all seven targets, but the task rule
  for the shop is that any red probe construct means explicit rows,
  so the view builds its three rows with `products.get(0/1/2, ...)`
  until the emitter bug is fixed.
- Type annotations take bare names only. `list[shop_shared.Product]`
  is a parse error (E1001), so the importing files name the structs
  `Product`, `CartLine`, and `CartRequest` in annotations; the
  checker resolves the bare name to the imported struct, and
  construction stays qualified (`shop_shared.Product(...)`).
- An imported const reference cannot be emitted yet (E4001 at build
  time, although `newr check` accepts it), so the `http.route` calls
  use string literals that match the `shop_shared` constants exactly;
  the constants stay the contract record.
- Each backend handler opens its own db connection and closes it
  before returning. The runtime binds a sqlite connection to the
  thread that opened it, and `http.serve` runs handlers on worker
  threads, so the top-level handle (opened on the main thread for
  the seed) cannot be used inside a handler; sharing it raises a
  thread error at request time.
- The view handlers are let-bound lambdas, not named fns passed
  directly and not inline lambdas. Two emitter limits force the
  shape: a named void fn passed as a fn value fails `javac`
  (the java emitter passes a bare method reference, which does not
  convert to a `Void`-returning fn type), and an inline lambda
  that calls a fetching fn fails the dart emit (E4001: dart only
  allows a fetching lambda as a route handler argument or a
  let-bound lambda). The let-bound form passes the emit on all
  seven targets and `javac` on java.
- The js frontend fetches the products once at start, so the backend
  must already be running when the frontend starts. If the backend
  restarts with different products, restart the frontend too.
