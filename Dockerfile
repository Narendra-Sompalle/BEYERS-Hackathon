FROM public.ecr.aws/lambda/python:3.12

# Copy requirements.txt
COPY requirements.txt ${LAMBDA_TASK_ROOT}

# Install the specified packages
RUN pip install -r requirements.txt

# Copy function code
COPY app ${LAMBDA_TASK_ROOT}/app
COPY src ${LAMBDA_TASK_ROOT}/src
# Copy other potentially needed directories (based on workspace)
COPY infra ${LAMBDA_TASK_ROOT}/infra
COPY seeder ${LAMBDA_TASK_ROOT}/seeder
COPY mock_data ${LAMBDA_TASK_ROOT}/mock_data

# Set the CMD to your handler
CMD [ "app.handler.lambda_handler" ]
