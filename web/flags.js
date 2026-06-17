// Team -> ISO code for flag images (flagcdn.com). Flags render identically on
// every OS (Windows doesn't draw emoji flags), so we use small PNGs sized to 1em.
const ISO = {
  "Spain": "es", "France": "fr", "England": "gb-eng", "Brazil": "br",
  "Argentina": "ar", "Portugal": "pt", "Germany": "de", "Netherlands": "nl",
  "Norway": "no", "Belgium": "be", "Colombia": "co", "Morocco": "ma",
  "Uruguay": "uy", "USA": "us", "Switzerland": "ch", "Japan": "jp",
  "Ecuador": "ec", "Croatia": "hr", "Mexico": "mx", "Senegal": "sn",
  "Turkey": "tr", "Sweden": "se", "Austria": "at", "Scotland": "gb-sct",
  "Canada": "ca", "Czechia": "cz", "Ivory Coast": "ci", "Ghana": "gh",
  "Egypt": "eg", "Paraguay": "py", "Algeria": "dz", "South Korea": "kr",
  "Tunisia": "tn", "Bosnia": "ba", "Australia": "au", "Iran": "ir",
  "DR Congo": "cd", "South Africa": "za", "Cape Verde": "cv",
  "Saudi Arabia": "sa", "Panama": "pa", "Uzbekistan": "uz", "Qatar": "qa",
  "New Zealand": "nz", "Iraq": "iq", "Haiti": "ht", "Curacao": "cw",
  "Jordan": "jo",
};

function getFlag(name) {
  const c = ISO[name];
  if (!c) return '<span class="flagimg-na">\u{1F3F3}️</span>';
  return `<img class="flagimg" loading="lazy" alt="" ` +
    `src="https://flagcdn.com/h40/${c}.png" ` +
    `srcset="https://flagcdn.com/h80/${c}.png 2x">`;
}
