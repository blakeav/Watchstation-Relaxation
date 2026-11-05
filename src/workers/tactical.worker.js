import { recommend } from "../adapters/coaEngine.js";

self.onmessage = (event) => {
  const message = event.data;
  if (!message || message.type !== "process") return;
  const view = new DataView(message.buffer);
  const count = view.getUint32(0, true);
  const floats = new Float32Array(message.buffer, 4);
  const tracks = new Array(count);
  let missile = false;
  for (let index = 0; index < count; index += 1) {
    const offset = index * 8;
    const bearing = floats[offset + 1];
    const rangeNm = floats[offset + 2];
    const domain = floats[offset + 6];
    const rad = (bearing * Math.PI) / 180;
    if (domain === 2 && rangeNm < 45) missile = true;
    tracks[index] = {
      id: floats[offset],
      bearing,
      rangeNm,
      course: floats[offset + 3],
      speed: floats[offset + 4],
      affiliation: floats[offset + 5],
      domain,
      quality: floats[offset + 7],
      east: rangeNm * Math.sin(rad),
      north: rangeNm * Math.cos(rad),
    };
  }
  const coa = recommend(message.ownship, message.radar, message.prefer || "primary");
  self.postMessage({
    seq: message.seq,
    tracks,
    coa,
    summary: { missile, trackCount: count },
  });
};
