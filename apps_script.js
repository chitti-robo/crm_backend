function onFormSubmit(e) {
  UrlFetchApp.fetch('https://api.yourdomain.com/api/public/google-forms', {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Api-Key': 'SECRET_KEY' },
    payload: JSON.stringify({ form: 'DS-Demo', answers: e.namedValues })
  });
}
