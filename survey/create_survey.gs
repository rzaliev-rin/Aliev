/**
 * Создаёт Google Форму «Бот Метрика_оценка АСП — обратная связь»
 * и таблицу для ответов. Запуск: выберите функцию createSurvey и нажмите «Выполнить».
 * Ссылки на форму и таблицу появятся в «Журнале выполнения» внизу.
 */
function createSurvey() {
  var form = FormApp.create('Бот «Метрика_оценка АСП» — обратная связь');
  form.setDescription(
    'EXPOCAR · Помогите сделать бота удобнее. Опрос займёт 3–5 минут.\n' +
    'Опрос анонимный: имя можно указать по желанию в конце. Нам важны и плюсы, и проблемы.');
  form.setCollectEmail(false);
  try { form.setRequireLogin(false); } catch (e) {}       // только для корпоративных аккаунтов
  form.setAllowResponseEdits(false);
  form.setLimitOneResponsePerUser(false);
  form.setShowLinkToRespondAgain(false);
  form.setProgressBar(true);
  form.setConfirmationMessage('Спасибо! Ваши ответы помогут сделать бота лучше. Администратор — Ринат Алиев.');

  // ---------- Раздел 1. О вас и общая оценка ----------
  form.addSectionHeaderItem().setTitle('О вас');
  var role = form.addMultipleChoiceItem().setTitle('Ваша роль').setRequired(true);
  form.addListItem().setTitle('Автосалон').setRequired(true).setChoiceValues([
    'Краснодар Changan', 'Волгоград Chery', 'Казань Chery', 'Новосибирск Chery',
    'Нижний Новгород Chery', 'Тюмень Exeed', 'Тюмень Toyota', 'Екатеринбург Toyota', 'Другой / несколько салонов']);
  form.addMultipleChoiceItem().setTitle('Как часто пользуетесь ботом?').setRequired(true).setChoiceValues([
    'Каждый день', 'Несколько раз в неделю', 'Реже раза в неделю', 'Пока не пользовался']);

  form.addSectionHeaderItem().setTitle('Общая оценка');
  form.addScaleItem().setTitle('Насколько вы довольны ботом в целом?').setBounds(1, 5)
    .setLabels('Совсем не доволен', 'Полностью доволен').setRequired(true);
  form.addScaleItem().setTitle('Насколько вероятно, что вы порекомендуете бота коллеге?').setBounds(0, 10)
    .setLabels('Точно нет', 'Точно да').setRequired(true);
  form.addMultipleChoiceItem().setTitle('Стало ли быстрее согласовывать оценки, чем раньше?').setChoiceValues([
    'Заметно быстрее', 'Немного быстрее', 'Так же', 'Медленнее', 'Затрудняюсь ответить']);
  form.addParagraphTextItem().setTitle('Комментарий к общей оценке');

  // ---------- Раздел 3а. Оценщикам ----------
  var pageAppraiser = form.addPageBreakItem().setTitle('Вопросы для оценщиков');
  form.addGridItem().setTitle('Насколько удобны функции бота?')
    .setRows(['Оценка по ссылке из MaxPoster', 'Автоподстановка данных из MaxPoster', 'Предварительный расчёт',
              '«Мои оценки»', 'Уведомления о решении по сделке', 'Кнопки ответа на запросы ПАЦ'])
    .setColumns(['Очень удобно', 'Удобно', 'Нейтрально', 'Неудобно', 'Не пользуюсь']);
  form.addMultipleChoiceItem().setTitle('Согласны ли вы с расчётом бота (категория, ПЦП, лимит)?').setChoiceValues([
    'Всегда', 'Чаще да', 'Иногда', 'Часто нет']);
  form.addCheckboxItem().setTitle('Где чаще всего расходится?').setChoiceValues([
    'Категория', 'Окрасы', 'Тип приёма', 'Пробег / год', 'ПЦП', 'Лимит / статус согласования', 'Не расходится'])
    .showOtherOption(true);
  form.addParagraphTextItem().setTitle('Опишите пример расхождения')
    .setHelpText('Можно вставить ссылку на оценку в MaxPoster');

  // ---------- Раздел 3б. РОП / ДДЦ / УК ----------
  var pageApprover = form.addPageBreakItem().setTitle('Вопросы для РОП, ДДЦ и УК');
  form.addScaleItem().setTitle('Удобно ли согласовывать сделки в чате и в личке?').setBounds(1, 5)
    .setLabels('Неудобно', 'Очень удобно');
  form.addMultipleChoiceItem().setTitle('Хватает ли информации в карточке, чтобы принять решение?')
    .setChoiceValues(['Да', 'Частично', 'Нет']);
  form.addParagraphTextItem().setTitle('Чего не хватает в карточке согласования?');
  form.addGridItem().setTitle('Насколько полезны функции?')
    .setRows(['Напоминания', 'Утренняя сводка', 'История оценок', 'Отчёт'])
    .setColumns(['Полезно', 'Нейтрально', 'Бесполезно', 'Не пользуюсь']);

  // ---------- Раздел 3в. ПАЦ ----------
  var pagePats = form.addPageBreakItem().setTitle('Вопросы для ПАЦ');
  form.addScaleItem().setTitle('Удобно ли подтверждать сделки в боте?').setBounds(1, 5)
    .setLabels('Неудобно', 'Очень удобно');
  form.addMultipleChoiceItem().setTitle('Хватает ли данных в карточке ПАЦ?').setChoiceValues(['Да', 'Частично', 'Нет']);
  form.addParagraphTextItem().setTitle('Чего не хватает в карточке ПАЦ?');
  form.addMultipleChoiceItem()
    .setTitle('Помогают ли кнопки «Запросить диагностику / исправление категории» и ответ оценщика?')
    .setChoiceValues(['Да', 'Частично', 'Нет', 'Не пользуюсь']);

  // ---------- Раздел 4. Проблемы ----------
  var pageProblems = form.addPageBreakItem().setTitle('Проблемы');
  form.addCheckboxItem().setTitle('С чем вы сталкивались?').setChoiceValues([
    'Не было доступа или долго ждал одобрения', 'Бот не нашёл оценку', 'Карточка не пришла в чат',
    'Кнопки не срабатывали', 'Неверные данные из MaxPoster', 'Бот отвечает медленно',
    'Непонятно, что делать дальше', 'Слишком много уведомлений', 'Ничего из перечисленного'])
    .showOtherOption(true);
  form.addParagraphTextItem().setTitle('Опишите случай подробнее');

  // ---------- Раздел 5. Предложения ----------
  form.addPageBreakItem().setTitle('Предложения');
  form.addParagraphTextItem().setTitle('Что вам больше всего нравится в боте?');
  form.addParagraphTextItem().setTitle('Что улучшить в первую очередь?');
  form.addCheckboxItem().setTitle('Каких функций не хватает?').setChoiceValues([
    'Фото авто в карточке', 'Расчёт по нескольким вариантам цены',
    'Напоминания оценщику о незавершённых оценках', 'Выгрузка истории в Excel'])
    .showOtherOption(true);
  var contact = form.addMultipleChoiceItem().setTitle('Можно ли связаться с вами для уточнений?');
  contact.setChoiceValues(['Да', 'Нет']);
  form.addTextItem().setTitle('Если да — имя и телефон или ID в MAX');

  // ---------- Ветвление по роли ----------
  role.setChoices([
    role.createChoice('Оценщик', pageAppraiser),
    role.createChoice('РОП', pageApprover),
    role.createChoice('ДДЦ', pageApprover),
    role.createChoice('УК', pageApprover),
    role.createChoice('ПАЦ', pagePats),
    role.createChoice('Другая роль', pageProblems)
  ]);
  // после своей ветки — сразу к «Проблемам»
  pageApprover.setGoToPage(pageProblems);  // конец ветки оценщиков
  pagePats.setGoToPage(pageProblems);      // конец ветки РОП/ДДЦ/УК

  // ---------- Таблица для ответов ----------
  var ss = SpreadsheetApp.create('Бот Метрика — ответы на опрос');
  form.setDestination(FormApp.DestinationType.SPREADSHEET, ss.getId());

  Logger.log('ССЫЛКА ДЛЯ СОТРУДНИКОВ: ' + form.shortenFormUrl(form.getPublishedUrl()));
  Logger.log('Редактировать форму: ' + form.getEditUrl());
  Logger.log('Таблица с ответами: ' + ss.getUrl());
}
