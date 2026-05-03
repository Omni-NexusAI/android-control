export default async function(ctx) {
  // Widen modal when opening droidclaw panels
  if (ctx.modalPath && ctx.modalPath.includes('droidclaw')) {
    // Add a class to the modal for CSS targeting
    if (ctx.modal) {
      ctx.modal.classList.add('droidclaw-wide-modal');
    }
    // Also modify the modal-content if accessible
    setTimeout(() => {
      const modalContent = document.querySelector('.modal-content');
      if (modalContent) {
        modalContent.style.maxWidth = '90vw';
        modalContent.style.width = '720px';
      }
    }, 10);
  }
}
