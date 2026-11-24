// Experimental policy, not a promoted default. Validate on target hardware.
// The native host preserves credentials and the server-owned shared Surf path.
var hooks = {
  configure: function(p) { return p; },
  playbackAttempt: function(input) {
    var p = input.policy;
    if (input.shared) return p;
    p.diagnosticSeconds = 8;
    if (input.attempt > 1) {
      p.segmentContainer = "mp4";
      p.segmentSeconds = 3;
      p.minimumSegments = 2;
      p.forceEncode = true;
    }
    return p;
  }
};
