async page => {
    const errors = [];
    page.on('response', response => {
        if (response.url().startsWith('http://127.0.0.1:8766') && response.status() >= 400) {
            errors.push(response.url());
        }
    });
    for (const layout of ['hierarchical', 'flat']) {
        await page.goto(`http://127.0.0.1:8766/${layout}/index.html`);
        await page.waitForFunction(() => document.querySelector('#content h1')?.textContent === 'overview');
        const storage = layout === 'flat' ? '存储.md' : '身份认证/会话/存储.md';
        await page.locator('.nav-group.collapsed > .nav-row > .nav-toggle').first().click();
        await page.locator(`.nav-item[data-file="${storage}"]`).click();
        await page.waitForFunction(() => document.querySelector('#content h1')?.textContent === '存储');
        await page.reload();
        await page.waitForFunction(() => document.querySelector('#content h1')?.textContent === '存储');
        await page.locator('#pager .pager-next').click();
        await page.waitForFunction(() => document.querySelector('#content h1')?.textContent === 'SDK配置');
        await page.locator('#content a').filter({ hasText: '存储' }).click();
        await page.waitForFunction(() => decodeURIComponent(location.hash).endsWith(':接口'));
        await page.waitForFunction(() => document.querySelector('#content h1')?.textContent === '存储');
        const anchor = await page.evaluate(() => parseHash().anchor);
        if (anchor !== '接口') throw new Error(`Unexpected decoded anchor: ${anchor}`);
        await page.locator('#pager .pager-prev').click();
        await page.waitForFunction(() => document.querySelector('#content h1')?.textContent === '会话');
        await page.locator('#content a').filter({ hasText: '配置' }).click();
        await page.waitForFunction(() => document.querySelector('#content h1')?.textContent === 'SDK配置');
        console.log(`${layout}: Chinese navigation, reload, pager, encoded links and anchors passed`);
    }
    if (errors.length) {
        throw new Error(`Local HTTP errors: ${errors.join(', ')}`);
    }
    console.log('Local HTTP errors: 0');
}
