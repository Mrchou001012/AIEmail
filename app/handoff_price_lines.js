(function (root, factory) {
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  root.AdminPriceLines = exported;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  class PriceLineValidationError extends Error {
    constructor(message) {
      super(message);
      this.name = "PriceLineValidationError";
    }
  }

  function collectPriceLines(container) {
    const lines = [];
    const labels = [];
    const productIds = new Set();

    for (const row of container.querySelectorAll("[data-price-line]")) {
      const select = row.querySelector(".manual-price-product");
      const productRaw = select.value.trim();
      const priceRaw = row.querySelector(".manual-price-value").value.trim();
      const quantityRaw = row.querySelector(".manual-price-quantity").value.trim();
      if (!productRaw && !priceRaw && !quantityRaw) continue;
      if (!productRaw) throw new PriceLineValidationError("Select a product");

      const productId = Number(productRaw);
      const price = Number(priceRaw);
      const quantity = Number(quantityRaw);
      if (!Number.isInteger(productId) || productId <= 0) {
        throw new PriceLineValidationError("Select a product");
      }
      if (!Number.isFinite(price) || price <= 0) {
        throw new PriceLineValidationError("Price must be greater than zero");
      }
      if (!Number.isInteger(quantity) || quantity <= 0) {
        throw new PriceLineValidationError("Quantity must be a positive integer");
      }
      if (productIds.has(productId)) {
        throw new PriceLineValidationError("Duplicate products are not allowed");
      }
      productIds.add(productId);

      const selected = select.selectedOptions[0];
      const label = selected ? selected.textContent : String(productId);
      lines.push({product_id: productId, standard_price: price, quantity});
      labels.push(`${label}: INR ${price} x ${quantity}`);
    }
    if (!lines.length) {
      throw new PriceLineValidationError("Add at least one complete product line");
    }
    return {lines, labels};
  }

  return {collectPriceLines, PriceLineValidationError};
});
