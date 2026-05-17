export const QUOTES = [
  "Maidenless behavior won't be tolerated.",
  "Foul Tarnished, in search of the Elden Ring. Emboldened by the flame of ambition, someone must extinguish thy flame. Let it be Margit, the Fell!",
  "Brave Tarnished. Take my hand, will you not?",
  "Stand up, be recognized. You are dauntless. No, perhaps you are simply… a lord.",
  "Hand of Marika, in this world, fingers are mere tools.",
  "Praise the message.",
  "But verily, it be the lord's, alone, to wield the lordship.",
  "Long live the king.",
  "A lord… a lord requires a lady. And a lady requires a lord.",
  "Well, well. What do we have here? Are you a Tarnished, too?",
];

export function randomQuote() {
  return QUOTES[Math.floor(Math.random() * QUOTES.length)];
}
