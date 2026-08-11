(function (root, factory) {
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  root.AdminRecordsRequests = exported;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function createRequestCoordinator() {
    let sequence = 0;
    let current = null;
    return {
      begin(snapshot) {
        if (current) current.controller.abort();
        const controller = new AbortController();
        current = {
          id: ++sequence,
          snapshot: Object.freeze({...snapshot}),
          controller,
          signal: controller.signal,
        };
        return current;
      },
      isCurrent(request) {
        return Boolean(
          current &&
          request.id === current.id &&
          !request.signal.aborted
        );
      },
    };
  }

  return {createRequestCoordinator};
});
