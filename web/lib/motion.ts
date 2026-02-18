export const revealUp = {
  hidden: { opacity: 0, y: 24 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.5, ease: [0.22, 1, 0.36, 1] }
  }
};

export const shortHover = {
  whileHover: { y: -3, scale: 1.01 },
  transition: { duration: 0.2 }
};
