/** Shared watchstander records.

Field names line up with the radar monitor in analyze_radar.py
(jammer_at / casualty_at) and the notional action codes in the
watchstation-relaxation module advanced_radar_ew.py:
0 contested face, 1 spatial null, 2 maneuver, 3 burn-through.
*/

export const Affiliation = {
  UNKNOWN: 0,
  FRIEND: 1,
  HOSTILE: 2,
  NEUTRAL: 3,
};

export const Domain = {
  SURFACE: 0,
  AIR: 1,
  MISSILE: 2,
};

export const Tier = {
  CRITICAL: "CRITICAL",
  WARNING: "WARNING",
  CAUTION: "CAUTION",
  INFORMATIONAL: "INFORMATIONAL",
};

export const RANGE_SCALES_NM = [10, 25, 50, 100];

/**
 * @typedef {Object} Track
 * @property {number} id
 * @property {number} bearing
 * @property {number} rangeNm
 * @property {number} course
 * @property {number} speed
 * @property {number} affiliation
 * @property {number} domain
 * @property {number} quality
 */

/**
 * @typedef {Object} JammingVector
 * @property {number} bearing
 * @property {number} half
 * @property {"noise"|"deception"} kind
 * @property {number} power
 * @property {number} factor
 * @property {boolean} primaryFace
 */

/**
 * @typedef {Object} ShipState
 * @property {number} heading
 * @property {number} speed
 * @property {number} radarRangeNm
 * @property {number|null} orderedHeading
 * @property {number|null} orderedSpeed
 */

/**
 * @typedef {Object} Alert
 * @property {string} id
 * @property {number} time
 * @property {string} tier
 * @property {string} code
 * @property {string} title
 * @property {string} detail
 * @property {boolean} acked
 * @property {boolean} cleared
 */
