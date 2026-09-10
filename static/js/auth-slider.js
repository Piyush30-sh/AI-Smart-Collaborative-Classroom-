/* Double slider auth interactions and client-side validation. */
(function initAuthSlider() {
  const slider = document.getElementById('authSlider');
  if (!slider) return;

  const validationMessage = 'Sign Up Failed: Please enter a valid email and a password with at least 8 characters.';
  const showSignup = () => slider.classList.add('slider-auth--signup-active');
  const showSignin = () => slider.classList.remove('slider-auth--signup-active');

  document.querySelectorAll('[data-show-signup]').forEach((button) => button.addEventListener('click', showSignup));
  document.querySelectorAll('[data-show-signin]').forEach((button) => button.addEventListener('click', showSignin));

  document.querySelectorAll('.slider-password-toggle').forEach((button) => {
    button.addEventListener('click', () => {
      const input = button.parentElement.querySelector('input');
      const icon = button.querySelector('i');
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      button.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
      icon.classList.toggle('fa-eye', showing);
      icon.classList.toggle('fa-eye-slash', !showing);
    });
  });

  const signupPassword = document.querySelector('[data-auth-form="signup"] input[name="password"]');
  const strengthFill = document.querySelector('.slider-strength__track span');
  const strengthLabel = document.querySelector('[data-strength-label]');
  if (signupPassword && strengthFill && strengthLabel) {
    signupPassword.addEventListener('input', () => {
      const value = signupPassword.value;
      let score = 0;
      if (value.length >= 8) score += 1;
      if (value.length >= 12) score += 1;
      if (/[A-Z]/.test(value)) score += 1;
      if (/[0-9]/.test(value)) score += 1;
      if (/[^A-Za-z0-9]/.test(value)) score += 1;

      const levels = [
        { width: '0%', color: '#cbd5e1', label: 'Password strength' },
        { width: '20%', color: '#ef4444', label: 'Very weak' },
        { width: '40%', color: '#f97316', label: 'Weak' },
        { width: '60%', color: '#eab308', label: 'Fair' },
        { width: '80%', color: '#22c55e', label: 'Strong' },
        { width: '100%', color: '#16a34a', label: 'Very strong' },
      ];
      const level = levels[score];
      strengthFill.style.width = level.width;
      strengthFill.style.background = level.color;
      strengthLabel.textContent = level.label;
    });
  }

  document.querySelectorAll('[data-auth-form]').forEach((form) => {
    const error = form.querySelector('.slider-error');
    form.addEventListener('submit', (event) => {
      const email = form.querySelector('input[name="email"]');
      const password = form.querySelector('input[name="password"]');
      const emailValid = email && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.value.trim());
      const passwordValid = password && password.value.length >= 8;
      let valid = emailValid && passwordValid;

      if (form.dataset.authForm === 'signup') {
        const firstName = form.querySelector('[data-first-name]');
        const lastName = form.querySelector('[data-last-name]');
        const confirm = form.querySelector('[data-confirm-password]');
        const terms = form.querySelector('.slider-terms input');
        const fullName = form.querySelector('[data-full-name]');
        valid = valid && firstName.value.trim() && lastName.value.trim() && confirm.value === password.value && terms.checked;
        if (fullName) fullName.value = `${firstName.value.trim()} ${lastName.value.trim()}`.trim();
      }

      if (!valid) {
        event.preventDefault();
        error.textContent = validationMessage;
        error.classList.add('is-visible');
        return;
      }

      error.classList.remove('is-visible');
    });

    form.addEventListener('input', () => error.classList.remove('is-visible'));
  });
})();
