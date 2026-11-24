// Plain-language text for alerts and conditions. Nothing here adds certainty:
// the numbers and the confidence come from the server.

export const SEVERITY = {
  info: ["Information", "info"],
  watch: ["Watch", "watch"],
  warning: ["Warning", "warning"],
  critical: ["Critical", "critical"],
};
export const RANK = { info: 0, watch: 1, warning: 2, critical: 3 };

/** [why it matters, what to do], by event type. */
export const GUIDANCE = {
  freeze: ["Water left outside can freeze, and tender plants can be damaged.", "Bring in or cover plants, and drain hoses."],
  frost_risk_tonight: ["Clear, calm nights cool quickly and frost can damage plants.", "Consider covering plants before dark."],
  frost_likely: ["Surfaces such as windshields and steps may be icy.", "Take care on steps and allow time to clear glass."],
  rain_onset: ["This pattern often comes with rain, but the station cannot measure rain.", "Look outside before leaving laundry or windows open."],
  heat: ["Humidity makes it harder to cool down in this heat.", "Drink water, rest in the shade and check on others."],
  sensor_covered: ["Light and sky estimates are unreliable while the sensor is covered.", "Check the sensor for snow, leaves or debris."],
  sensor_stuck: ["Temperature and humidity may no longer be real readings.", "Restart the tag by refitting its battery."],
  tag_offline: ["New readings are not arriving, so the values shown are the last known ones.", "Check the tag's battery and its distance from the receiver."],
  test: ["It shows that alerts reach this page.", ""],
};

/** [label, icon] of the estimated sky. */
export const SKY = {
  clear: ["Clear sky", "sun"],
  partly_cloudy: ["Partly cloudy", "cloudSun"],
  mostly_cloudy: ["Mostly cloudy", "cloud"],
  overcast: ["Overcast", "cloud"],
  very_bright: ["Very bright", "sun"],
  night: ["Night", "moon"],
  dawn: ["Dawn", "horizon"],
  dusk: ["Dusk", "horizon"],
};

export const signalLabel = (dbm) => (dbm >= -70 ? "good" : dbm >= -85 ? "fair" : "weak");
