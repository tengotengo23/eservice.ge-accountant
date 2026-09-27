-- მხოლოდ docker-compose dev გარემოსთვის. prod-ში როლი და პაროლი იქმნება secret manager-იდან.
-- აპლიკაცია უკავშირდება ამ როლით (არა superuser-ით), რათა Row Level Security იმოქმედოს.
CREATE ROLE ai_accountant_app LOGIN PASSWORD 'dev-only-change-me';
