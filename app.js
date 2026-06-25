document.addEventListener('DOMContentLoaded', () => {
    // State management
    let allStocks = [];
    let currentFilter = 'all';
    let searchQuery = '';
    let sortColumn = 'Appearance_Count';
    let sortDirection = 'desc';

    // Sorting helper
    function sortData(stocks) {
        if (!sortColumn) return stocks;
        
        return [...stocks].sort((a, b) => {
            let valA = a[sortColumn];
            let valB = b[sortColumn];
            
            // Handle null / undefined / NaN values
            const isNullA = valA === null || valA === undefined || (typeof valA === 'number' && isNaN(valA));
            const isNullB = valB === null || valB === undefined || (typeof valB === 'number' && isNaN(valB));
            
            if (isNullA && isNullB) return 0;
            if (isNullA) return 1; // Null values always go to the bottom
            if (isNullB) return -1;
            
            let comparison = 0;
            if (typeof valA === 'string' && typeof valB === 'string') {
                comparison = valA.localeCompare(valB, 'ko');
            } else {
                comparison = Number(valA) - Number(valB);
            }
            
            return sortDirection === 'asc' ? comparison : -comparison;
        });
    }

    // Update Header UI with sort indicators
    function updateHeaderUI() {
        const headers = document.querySelectorAll('#stocks-table th[data-sort]');
        headers.forEach(th => {
            const col = th.getAttribute('data-sort');
            const icon = th.querySelector('i');
            if (!icon) return;
            
            if (col === sortColumn) {
                th.classList.add('active-sort');
                if (sortDirection === 'asc') {
                    icon.className = 'fa-solid fa-sort-up sort-icon-header';
                } else {
                    icon.className = 'fa-solid fa-sort-down sort-icon-header';
                }
            } else {
                th.classList.remove('active-sort');
                icon.className = 'fa-solid fa-sort sort-icon-header';
            }
        });
    }

    // Element bindings
    const tableBody = document.getElementById('table-body');
    const totalCountEl = document.getElementById('total-stocks-count');
    const maxAppearingEl = document.getElementById('max-appearing-count');
    const lastUpdatedEl = document.getElementById('last-updated');
    const searchInput = document.getElementById('search-input');
    const filterButtons = document.querySelectorAll('.filter-btn');

    // Number format helpers
    function formatPrice(val) {
        if (val === null || val === undefined) return '-';
        return Number(val).toLocaleString() + '원';
    }

    function formatMarcap(val) {
        if (val === null || val === undefined) return '-';
        const num = Number(val);
        const jo = Math.floor(num / 1000000000000);
        const eok = Math.floor((num % 1000000000000) / 100000000);
        
        if (jo > 0) {
            return `${jo}조 ${eok.toLocaleString()}억원`;
        }
        return `${eok.toLocaleString()}억원`;
    }

    function formatFinancial(val) {
        if (val === null || val === undefined) return '-';
        // Revenue & Operating Income are stored in 100M KRW (억원)
        const num = Number(val);
        const jo = Math.floor(num / 10000);
        const eok = Math.floor(num % 10000);
        
        if (jo > 0) {
            return `${jo}조 ${eok.toLocaleString()}억원`;
        }
        return `${eok.toLocaleString()}억원`;
    }

    function formatPercent(val) {
        if (val === null || val === undefined) return '-';
        return Number(val).toFixed(2) + '%';
    }

    function formatRatio(val) {
        if (val === null || val === undefined) return '-';
        return Number(val).toFixed(2) + '배';
    }

    function formatExcessReturn(val) {
        if (val === null || val === undefined) return '-';
        // Backend stores return as a decimal ratio (e.g. 0.1996 for 19.96%, 1.3684 for 136.84%)
        // Always multiply by 100 to show correct percentage.
        let percent = Number(val) * 100;
        const sign = percent > 0 ? '+' : '';
        const color = percent > 0 ? 'accent-green' : (percent < 0 ? 'accent-red' : '');
        return `<span style="color: var(--${color || 'text-primary'}); font-weight: 600;">${sign}${percent.toFixed(2)}%</span>`;
    }

    // Badge styling for Appearance Count
    function getAppearanceBadge(count) {
        const num = Number(count);
        if (num === 4) {
            return `<span class="badge gold"><i class="fa-solid fa-fire"></i> 4주 연속</span>`;
        } else if (num === 3) {
            return `<span class="badge purple">3주 출현</span>`;
        } else if (num === 2) {
            return `<span class="badge blue">2주 출현</span>`;
        }
        return `<span class="badge grey">1주 신규</span>`;
    }

    // Fetch and Load data
    async function loadData() {
        try {
            // Fetch the compiled data from weekly_results/data.json
            const response = await fetch('weekly_results/data.json');
            if (!response.ok) {
                throw new Error('데이터 파일을 불러오는 데 실패했습니다.');
            }
            
            const data = await response.json();
            
            // Set update timestamp
            if (data.last_updated) {
                lastUpdatedEl.innerHTML = `<i class="fa-regular fa-clock"></i> 업데이트: ${data.last_updated}`;
            }
            
            allStocks = data.stocks || [];
            
            // Update stats layout
            totalCountEl.textContent = `${allStocks.length}개`;
            
            // Update top sectors
            const topSectorsEl = document.getElementById('top-sectors-list');
            if (topSectorsEl && data.top_sectors) {
                if (data.top_sectors.length > 0) {
                    topSectorsEl.innerHTML = data.top_sectors.map((sec, idx) => {
                        const colors = ['gold', 'purple', 'blue'];
                        const color = colors[idx] || 'grey';
                        return `<span class="sector-badge ${color}">${idx + 1}위: ${sec}</span>`;
                    }).join('');
                } else {
                    topSectorsEl.innerHTML = `<span class="sector-badge grey">데이터가 없습니다</span>`;
                }
            }
            
            // Render table initially
            updateHeaderUI();
            renderTable();
            
        } catch (error) {
            console.error(error);
            tableBody.innerHTML = `
                <tr>
                    <td colspan="10" class="empty-state">
                        <i class="fa-solid fa-triangle-exclamation"></i>
                        데이터를 로드하지 못했습니다.<br>
                        <span style="font-size: 0.9rem; color: var(--text-muted); margin-top: 0.5rem; display: block;">
                            (${error.message})
                        </span>
                    </td>
                </tr>
            `;
        }
    }

    // Render table rows based on filter & search query
    function renderTable() {
        // Clear table first
        tableBody.innerHTML = '';
        
        // Filter stocks
        let filtered = allStocks.filter(stock => {
            // 1. Search Query filtering
            const sQuery = searchQuery.toLowerCase();
            const matchesSearch = 
                stock.Name.toLowerCase().includes(sQuery) || 
                stock.Ticker.toLowerCase().includes(sQuery) || 
                (stock.Sector && stock.Sector.toLowerCase().includes(sQuery));
            
            if (!matchesSearch) return false;
            
            // 2. Tab Filter filtering
            if (currentFilter === '4') {
                return stock.Appearance_Count === 4;
            } else if (currentFilter === '3') {
                return stock.Appearance_Count >= 3;
            }
            return true;
        });

        // 3. Sort filtered stocks
        filtered = sortData(filtered);

        if (filtered.length === 0) {
            tableBody.innerHTML = `
                <tr>
                    <td colspan="10" class="empty-state">
                        <i class="fa-solid fa-folder-open"></i> 조건에 부합하는 종목이 없습니다.
                    </td>
                </tr>
            `;
            return;
        }

        // Render filtered rows
        filtered.forEach(stock => {
            const tr = document.createElement('tr');
            
            // Highlight classes for rows
            if (stock.Appearance_Count === 4) {
                tr.classList.add('highlight-4');
            } else if (stock.Appearance_Count === 3) {
                tr.classList.add('highlight-3');
            }
            
            const ticker = stock.Ticker;
            const naverUrl = `https://finance.naver.com/item/main.naver?code=${ticker}`;
            
            tr.innerHTML = `
                <td>
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                        <a href="${naverUrl}" target="_blank" class="stock-link">
                            ${stock.Name}
                        </a>
                        <span class="ticker-label">${ticker}</span>
                        <a href="${naverUrl}" target="_blank" style="color: var(--text-muted); font-size: 0.75rem;">
                            <i class="fa-solid fa-arrow-up-right-from-square"></i>
                        </a>
                    </div>
                </td>
                <td class="num-col">${formatPrice(stock.Current_Price)}</td>
                <td class="num-col">${formatMarcap(stock.Marcap)}</td>
                <td class="num-col">${formatFinancial(stock.Revenue)}</td>
                <td class="num-col">${formatFinancial(stock.Operating_Income)}</td>
                <td class="num-col" style="font-weight: 600;">${formatPercent(stock.ROE)}</td>
                <td class="num-col">${formatRatio(stock.PER)}</td>
                <td class="num-col">${formatRatio(stock.PBR)}</td>
                <td class="num-col">${formatExcessReturn(stock.Excess_Return_3M)}</td>
                <td class="center-col">${getAppearanceBadge(stock.Appearance_Count)}</td>
            `;
            
            tableBody.appendChild(tr);
        });
    }

    // Search event
    searchInput.addEventListener('input', (e) => {
        searchQuery = e.target.value;
        renderTable();
    });

    // Tab buttons event
    filterButtons.forEach(btn => {
        btn.addEventListener('click', (e) => {
            // Remove active class
            filterButtons.forEach(b => b.classList.remove('active'));
            
            // Add active class
            e.currentTarget.classList.add('active');
            
            // Update filter value
            currentFilter = e.currentTarget.getAttribute('data-filter');
            
            // Render table
            renderTable();
        });
    });

    // Sort events
    const headers = document.querySelectorAll('#stocks-table th[data-sort]');
    headers.forEach(th => {
        th.addEventListener('click', () => {
            const col = th.getAttribute('data-sort');
            if (sortColumn === col) {
                // Toggle direction
                sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
            } else {
                sortColumn = col;
                sortDirection = 'desc'; // Default to desc when clicking a new column
            }
            updateHeaderUI();
            renderTable();
        });
    });

    // Initialize application
    loadData();
});
